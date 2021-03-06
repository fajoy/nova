# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
# Copyright (c) 2010 Citrix Systems, Inc.
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
Handling of VM disk images.
"""

import os
import re

from nova import exception
from nova.image import glance
from nova.openstack.common import cfg
from nova.openstack.common import log as logging
from nova import utils


LOG = logging.getLogger(__name__)

image_opts = [
    cfg.BoolOpt('force_raw_images',
                default=True,
                help='Force backing images to raw format'),
]

CONF = cfg.CONF
CONF.register_opts(image_opts)


class QemuImgInfo(object):
    BACKING_FILE_RE = re.compile((r"^(.*?)\s*\(actual\s+path\s*:"
                                  r"\s+(.*?)\)\s*$"), re.I)
    TOP_LEVEL_RE = re.compile(r"^([\w\d\s\_\-]+):(.*)$")
    SIZE_RE = re.compile(r"\(\s*(\d+)\s+bytes\s*\)", re.I)

    def __init__(self, cmd_output):
        details = self._parse(cmd_output)
        self.image = details.get('image')
        self.backing_file = details.get('backing_file')
        self.file_format = details.get('file_format')
        self.virtual_size = details.get('virtual_size')
        self.cluster_size = details.get('cluster_size')
        self.disk_size = details.get('disk_size')
        self.snapshots = details.get('snapshot_list', [])
        self.encryption = details.get('encryption')

    def __str__(self):
        lines = [
            'image: %s' % self.image,
            'file_format: %s' % self.file_format,
            'virtual_size: %s' % self.virtual_size,
            'disk_size: %s' % self.disk_size,
            'cluster_size: %s' % self.cluster_size,
            'backing_file: %s' % self.backing_file,
        ]
        if self.snapshots:
            lines.append("snapshots: %s" % self.snapshots)
        return "\n".join(lines)

    def _canonicalize(self, field):
        # Standardize on underscores/lc/no dash and no spaces
        # since qemu seems to have mixed outputs here... and
        # this format allows for better integration with python
        # - ie for usage in kwargs and such...
        field = field.lower().strip()
        for c in (" ", "-"):
            field = field.replace(c, '_')
        return field

    def _extract_bytes(self, details):
        # Replace it with the byte amount
        real_size = self.SIZE_RE.search(details)
        if real_size:
            details = real_size.group(1)
        try:
            details = utils.to_bytes(details)
        except (TypeError, ValueError):
            pass
        return details

    def _extract_details(self, root_cmd, root_details, lines_after):
        consumed_lines = 0
        real_details = root_details
        if root_cmd == 'backing_file':
            # Replace it with the real backing file
            backing_match = self.BACKING_FILE_RE.match(root_details)
            if backing_match:
                real_details = backing_match.group(2).strip()
        elif root_cmd in ['virtual_size', 'cluster_size', 'disk_size']:
            # Replace it with the byte amount (if we can convert it)
            real_details = self._extract_bytes(root_details)
        elif root_cmd == 'file_format':
            real_details = real_details.strip().lower()
        elif root_cmd == 'snapshot_list':
            # Next line should be a header, starting with 'ID'
            if not lines_after or not lines_after[0].startswith("ID"):
                msg = _("Snapshot list encountered but no header found!")
                raise ValueError(msg)
            consumed_lines += 1
            possible_contents = lines_after[1:]
            real_details = []
            # This is the sprintf pattern we will try to match
            # "%-10s%-20s%7s%20s%15s"
            # ID TAG VM SIZE DATE VM CLOCK (current header)
            for line in possible_contents:
                line_pieces = line.split(None)
                if len(line_pieces) != 6:
                    break
                else:
                    # Check against this pattern occuring in the final position
                    # "%02d:%02d:%02d.%03d"
                    date_pieces = line_pieces[5].split(":")
                    if len(date_pieces) != 3:
                        break
                    real_details.append({
                        'id': line_pieces[0],
                        'tag': line_pieces[1],
                        'vm_size': line_pieces[2],
                        'date': line_pieces[3],
                        'vm_clock': line_pieces[4] + " " + line_pieces[5],
                    })
                    consumed_lines += 1
        return (real_details, consumed_lines)

    def _parse(self, cmd_output):
        # Analysis done of qemu-img.c to figure out what is going on here
        # Find all points start with some chars and then a ':' then a newline
        # and then handle the results of those 'top level' items in a separate
        # function.
        #
        # TODO(harlowja): newer versions might have a json output format
        #                 we should switch to that whenever possible.
        #                 see: http://bit.ly/XLJXDX
        if not cmd_output:
            cmd_output = ''
        contents = {}
        lines = cmd_output.splitlines()
        i = 0
        line_am = len(lines)
        while i < line_am:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            consumed_lines = 0
            top_level = self.TOP_LEVEL_RE.match(line)
            if top_level:
                root = self._canonicalize(top_level.group(1))
                if not root:
                    i += 1
                    continue
                root_details = top_level.group(2).strip()
                details, consumed_lines = self._extract_details(root,
                                                                root_details,
                                                                lines[i + 1:])
                contents[root] = details
            i += consumed_lines + 1
        return contents


def qemu_img_info(path):
    """Return a object containing the parsed output from qemu-img info."""
    if not os.path.exists(path):
        return QemuImgInfo()

    out, err = utils.execute('env', 'LC_ALL=C', 'LANG=C',
                             'qemu-img', 'info', path)
    return QemuImgInfo(out)


def convert_image(source, dest, out_format):
    """Convert image to other format"""
    cmd = ('qemu-img', 'convert', '-O', out_format, source, dest)
    utils.execute(*cmd)


def fetch(context, image_href, path, _user_id, _project_id):
    # TODO(vish): Improve context handling and add owner and auth data
    #             when it is added to glance.  Right now there is no
    #             auth checking in glance, so we assume that access was
    #             checked before we got here.
    (image_service, image_id) = glance.get_remote_image_service(context,
                                                                image_href)
    with utils.remove_path_on_error(path):
        with open(path, "wb") as image_file:
            image_service.download(context, image_id, image_file)


def fetch_to_raw(context, image_href, path, user_id, project_id):
    path_tmp = "%s.part" % path
    fetch(context, image_href, path_tmp, user_id, project_id)

    with utils.remove_path_on_error(path_tmp):
        data = qemu_img_info(path_tmp)

        fmt = data.file_format
        if fmt is None:
            raise exception.ImageUnacceptable(
                reason=_("'qemu-img info' parsing failed."),
                image_id=image_href)

        backing_file = data.backing_file
        if backing_file is not None:
            raise exception.ImageUnacceptable(image_id=image_href,
                reason=_("fmt=%(fmt)s backed by: %(backing_file)s") % locals())

        if fmt != "raw" and CONF.force_raw_images:
            staged = "%s.converted" % path
            LOG.debug("%s was %s, converting to raw" % (image_href, fmt))
            with utils.remove_path_on_error(staged):
                convert_image(path_tmp, staged, 'raw')

                data = qemu_img_info(staged)
                if data.file_format != "raw":
                    raise exception.ImageUnacceptable(image_id=image_href,
                        reason=_("Converted to raw, but format is now %s") %
                        data.file_format)

                os.rename(staged, path)

        else:
            os.rename(path_tmp, path)
