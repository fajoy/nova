# Copyright (c) 2012 Rackspace Hosting
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
CellState Manager
"""
import copy
import datetime
import functools

from nova.cells import rpc_driver
from nova import context
from nova.db import base
from nova.openstack.common import cfg
from nova.openstack.common import lockutils
from nova.openstack.common import log as logging
from nova.openstack.common import timeutils

cell_state_manager_opts = [
        cfg.IntOpt('db_check_interval',
                default=60,
                help='Seconds between getting fresh cell info from db.'),
]


LOG = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.import_opt('host', 'nova.config')
CONF.import_opt('name', 'nova.cells.opts', group='cells')
#CONF.import_opt('capabilities', 'nova.cells.opts', group='cells')
CONF.register_opts(cell_state_manager_opts, group='cells')


class CellState(object):
    """Holds information for a particular cell."""
    def __init__(self, cell_name, is_me=False):
        self.name = cell_name
        self.is_me = is_me
        self.last_seen = datetime.datetime.min
        self.capabilities = {}
        self.capacities = {}
        self.db_info = {}
        # TODO(comstud): The DB will specify the driver to use to talk
        # to this cell, but there's no column for this yet.  The only
        # available driver is the rpc driver.
        self.driver = rpc_driver.CellsRPCDriver()

    def update_db_info(self, cell_db_info):
        """Update cell credentials from db"""
        self.db_info = dict(
                [(k, v) for k, v in cell_db_info.iteritems()
                        if k != 'name'])

    def update_capabilities(self, cell_metadata):
        """Update cell capabilities for a cell."""
        self.last_seen = timeutils.utcnow()
        self.capabilities = cell_metadata

    def update_capacities(self, capacities):
        """Update capacity information for a cell."""
        self.last_seen = timeutils.utcnow()
        self.capacities = capacities

    def get_cell_info(self):
        """Return subset of cell information for OS API use."""
        db_fields_to_return = ['id', 'is_parent', 'weight_scale',
                'weight_offset', 'username', 'rpc_host', 'rpc_port']
        cell_info = dict(name=self.name, capabilities=self.capabilities)
        if self.db_info:
            for field in db_fields_to_return:
                cell_info[field] = self.db_info[field]
        return cell_info

    def send_message(self, message):
        """Send a message to a cell.  Just forward this to the driver,
        passing ourselves and the message as arguments.
        """
        self.driver.send_message_to_cell(self, message)

    def __repr__(self):
        me = "me" if self.is_me else "not_me"
        return "Cell '%s' (%s)" % (self.name, me)


def sync_from_db(f):
    """Use as a decorator to wrap methods that use cell information to
    make sure they sync the latest information from the DB periodically.
    """
    @functools.wraps(f)
    def wrapper(self, *args, **kwargs):
        if self._time_to_sync():
            self._cell_db_sync()
        return f(self, *args, **kwargs)
    return wrapper


class CellStateManager(base.Base):
    def __init__(self, cell_state_cls=None):
        super(CellStateManager, self).__init__()
        if not cell_state_cls:
            cell_state_cls = CellState
        self.cell_state_cls = cell_state_cls
        self.my_cell_state = cell_state_cls(CONF.cells.name, is_me=True)
        self.parent_cells = {}
        self.child_cells = {}
        self.last_cell_db_check = datetime.datetime.min
        self._cell_db_sync()
        my_cell_capabs = {}
        for cap in CONF.cells.capabilities:
            name, value = cap.split('=', 1)
            if ';' in value:
                values = set(value.split(';'))
            else:
                values = set([value])
            my_cell_capabs[name] = values
            self.my_cell_state.update_capabilities(my_cell_capabs)

    def _refresh_cells_from_db(self, ctxt):
        """Make our cell info map match the db."""
        # Add/update existing cells ...
        db_cells = self.db.cell_get_all(ctxt)
        db_cells_dict = dict([(cell['name'], cell) for cell in db_cells])

        # Update current cells.  Delete ones that disappeared
        for cells_dict in (self.parent_cells, self.child_cells):
            for cell_name, cell_info in cells_dict.items():
                is_parent = cell_info.db_info['is_parent']
                db_dict = db_cells_dict.get(cell_name)
                if db_dict and is_parent == db_dict['is_parent']:
                    cell_info.update_db_info(db_dict)
                else:
                    del cells_dict[cell_name]

        # Add new cells
        for cell_name, db_info in db_cells_dict.items():
            if db_info['is_parent']:
                cells_dict = self.parent_cells
            else:
                cells_dict = self.child_cells
            if cell_name not in cells_dict:
                cells_dict[cell_name] = self.cell_state_cls(cell_name)
                cells_dict[cell_name].update_db_info(db_info)

    def _time_to_sync(self):
        """Is it time to sync the DB against our memory cache?"""
        diff = timeutils.utcnow() - self.last_cell_db_check
        return diff.seconds >= CONF.cells.db_check_interval

    def _update_our_capacity(self, context):
        """Update our capacity in the self.my_cell_state CellState.

        This will add/update 2 entries in our CellState.capacities,
        'ram_free' and 'disk_free'.

        The values of these are both dictionaries with the following
        format:

        {'total_mb': <total_memory_free_in_the_cell>,
         'units_by_mb: <units_dictionary>}

        <units_dictionary> contains the number of units that we can
        build for every instance_type that we have.  This number is
        computed by looking at room available on every compute_node.

        Take the following instance_types as an example:

        [{'memory_mb': 1024, 'root_gb': 10, 'ephemeral_gb': 100},
         {'memory_mb': 2048, 'root_gb': 20, 'ephemeral_gb': 200}]

        capacities['ram_free']['units_by_mb'] would contain the following:

        {'1024': <number_of_instances_that_will_fit>,
         '2048': <number_of_instances_that_will_fit>}

        capacities['disk_free']['units_by_mb'] would contain the following:

        {'122880': <number_of_instances_that_will_fit>,
         '225280': <number_of_instances_that_will_fit>}

        Units are in MB, so 122880 = (10 + 100) * 1024.

        NOTE(comstud): Perhaps we should only report a single number
        available per instance_type.
        """

        compute_hosts = {}

        def _get_compute_hosts():
            compute_nodes = self.db.compute_node_get_all(context)
            for compute in compute_nodes:
                service = compute['service']
                if not service or service['disabled']:
                    continue
                host = service['host']
                compute_hosts[host] = {
                        'free_ram_mb': compute['free_ram_mb'],
                        'free_disk_mb': compute['free_disk_gb'] * 1024}

        _get_compute_hosts()
        if not compute_hosts:
            self.my_cell_state.update_capacities({})
            return

        ram_mb_free_units = {}
        disk_mb_free_units = {}
        total_ram_mb_free = 0
        total_disk_mb_free = 0

        def _free_units(tot, per_inst):
            if per_inst:
                return max(0, int(tot / per_inst))
            else:
                return 0

        def _update_from_values(values, instance_type):
            memory_mb = instance_type['memory_mb']
            disk_mb = (instance_type['root_gb'] +
                    instance_type['ephemeral_gb']) * 1024
            ram_mb_free_units.setdefault(str(memory_mb), 0)
            disk_mb_free_units.setdefault(str(disk_mb), 0)
            ram_free_units = _free_units(compute_values['free_ram_mb'],
                    memory_mb)
            disk_free_units = _free_units(compute_values['free_disk_mb'],
                    disk_mb)
            ram_mb_free_units[str(memory_mb)] += ram_free_units
            disk_mb_free_units[str(disk_mb)] += disk_free_units

        instance_types = self.db.instance_type_get_all(context)

        for compute_values in compute_hosts.values():
            total_ram_mb_free += compute_values['free_ram_mb']
            total_disk_mb_free += compute_values['free_disk_mb']
            for instance_type in instance_types:
                _update_from_values(compute_values, instance_type)

        capacities = {'ram_free': {'total_mb': total_ram_mb_free,
                                   'units_by_mb': ram_mb_free_units},
                      'disk_free': {'total_mb': total_disk_mb_free,
                                    'units_by_mb': disk_mb_free_units}}
        self.my_cell_state.update_capacities(capacities)

    @lockutils.synchronized('cell-db-sync', 'nova-')
    def _cell_db_sync(self):
        """Update status for all cells if it's time.  Most calls to
        this are from the check_for_update() decorator that checks
        the time, but it checks outside of a lock.  The duplicate
        check here is to prevent multiple threads from pulling the
        information simultaneously.
        """
        if self._time_to_sync():
            LOG.debug(_("Updating cell cache from db."))
            self.last_cell_db_check = timeutils.utcnow()
            ctxt = context.get_admin_context()
            self._refresh_cells_from_db(ctxt)
            self._update_our_capacity(ctxt)

    @sync_from_db
    def get_my_state(self):
        """Return information for my (this) cell."""
        return self.my_cell_state

    @sync_from_db
    def get_child_cells(self):
        """Return list of child cell_infos."""
        return self.child_cells.values()

    @sync_from_db
    def get_parent_cells(self):
        """Return list of parent cell_infos."""
        return self.parent_cells.values()

    @sync_from_db
    def get_parent_cell(self, cell_name):
        return self.parent_cells.get(cell_name)

    @sync_from_db
    def get_child_cell(self, cell_name):
        return self.child_cells.get(cell_name)

    @sync_from_db
    def update_cell_capabilities(self, cell_name, capabilities):
        """Update capabilities for a cell."""
        cell = self.child_cells.get(cell_name)
        if not cell:
            cell = self.parent_cells.get(cell_name)
        if not cell:
            LOG.error(_("Unknown cell '%(cell_name)s' when trying to "
                        "update capabilities"), locals())
            return
        # Make sure capabilities are sets.
        for capab_name, values in capabilities.items():
            capabilities[capab_name] = set(values)
        cell.update_capabilities(capabilities)

    @sync_from_db
    def update_cell_capacities(self, cell_name, capacities):
        """Update capacities for a cell."""
        cell = self.child_cells.get(cell_name)
        if not cell:
            cell = self.parent_cells.get(cell_name)
        if not cell:
            LOG.error(_("Unknown cell '%(cell_name)s' when trying to "
                        "update capacities"), locals())
            return
        cell.update_capacities(capacities)

    @sync_from_db
    def get_our_capabilities(self, include_children=True):
        capabs = copy.deepcopy(self.my_cell_state.capabilities)
        if include_children:
            for cell in self.child_cells.values():
                for capab_name, values in cell.capabilities.items():
                    if capab_name not in capabs:
                        capabs[capab_name] = set([])
                    capabs[capab_name] |= values
        return capabs

    def _add_to_dict(self, target, src):
        for key, value in src.items():
            if isinstance(value, dict):
                target.setdefault(key, {})
                self._add_to_dict(target[key], value)
                continue
            target.setdefault(key, 0)
            target[key] += value

    @sync_from_db
    def get_our_capacities(self, include_children=True):
        capacities = copy.deepcopy(self.my_cell_state.capacities)
        if include_children:
            for cell in self.child_cells.values():
                self._add_to_dict(capacities, cell.capacities)
        return capacities
