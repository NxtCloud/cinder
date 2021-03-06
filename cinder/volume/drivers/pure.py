# Copyright (c) 2014 Pure Storage, Inc.
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
Volume driver for Pure Storage FlashArray storage system.

This driver requires Purity version 3.4.0 or later.
"""

import cookielib
import json
import re
import urllib2
import uuid

from oslo.config import cfg
from oslo.utils import excutils
from oslo.utils import units
from oslo_concurrency import processutils

from cinder import exception
from cinder.i18n import _, _LE, _LI, _LW
from cinder.openstack.common import log as logging
from cinder import utils
from cinder.volume.drivers.san import san

LOG = logging.getLogger(__name__)

PURE_OPTS = [
    cfg.StrOpt("pure_api_token", default=None,
               help="REST API authorization token."),
]

CONF = cfg.CONF
CONF.register_opts(PURE_OPTS)

INVALID_CHARACTERS = re.compile(r"[^-a-zA-Z0-9]")
GENERATED_NAME = re.compile(r".*-[a-f0-9]{32}-cinder$")


def _get_vol_name(volume):
    """Return the name of the volume Purity will use."""
    return volume["name"] + "-cinder"


def _get_snap_name(snapshot):
    """Return the name of the snapshot that Purity will use."""
    return "%s-cinder.%s" % (snapshot["volume_name"], snapshot["name"])


def _generate_purity_host_name(name):
    """Return a valid Purity host name based on the name passed in."""
    if len(name) > 23:
        name = name[0:23]
    name = INVALID_CHARACTERS.sub("-", name)
    name = name.lstrip("-")
    return "{name}-{uuid}-cinder".format(name=name, uuid=uuid.uuid4().hex)


class PureISCSIDriver(san.SanISCSIDriver):
    """Performs volume management on Pure Storage FlashArray."""

    VERSION = "2.0.0"

    def __init__(self, *args, **kwargs):
        execute = kwargs.pop("execute", utils.execute)
        super(PureISCSIDriver, self).__init__(execute=execute, *args, **kwargs)
        self.configuration.append_config_values(PURE_OPTS)
        self._array = None
        self._iscsi_port = None
        self._backend_name = (self.configuration.volume_backend_name or
                              self.__class__.__name__)

    def do_setup(self, context):
        """Performs driver initialization steps that could raise exceptions."""
        # Raises PureDriverException if unable to connect and PureAPIException
        # if unable to authenticate.
        self._array = FlashArray(
            self.configuration.san_ip,
            self.configuration.pure_api_token)
        self._iscsi_port = self._choose_target_iscsi_port()

    def check_for_setup_error(self):
        # Avoid inheriting check_for_setup_error from SanDriver, which checks
        # for san_password or san_private_key, not relevant to our driver.
        pass

    def create_volume(self, volume):
        """Creates a volume."""
        LOG.debug("Enter PureISCSIDriver.create_volume.")
        vol_name = _get_vol_name(volume)
        vol_size = volume["size"] * units.Gi
        self._array.create_volume(vol_name, vol_size)
        LOG.debug("Leave PureISCSIDriver.create_volume.")

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot."""
        LOG.debug("Enter PureISCSIDriver.create_volume_from_snapshot.")
        vol_name = _get_vol_name(volume)
        snap_name = _get_snap_name(snapshot)
        self._array.copy_volume(snap_name, vol_name)
        self._extend_if_needed(vol_name, snapshot["volume_size"],
                               volume["size"])
        LOG.debug("Leave PureISCSIDriver.create_volume_from_snapshot.")

    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume."""
        LOG.debug("Enter PureISCSIDriver.create_cloned_volume.")
        vol_name = _get_vol_name(volume)
        src_name = _get_vol_name(src_vref)
        self._array.copy_volume(src_name, vol_name)
        self._extend_if_needed(vol_name, src_vref["size"], volume["size"])
        LOG.debug("Leave PureISCSIDriver.create_cloned_volume.")

    def _extend_if_needed(self, vol_name, src_size, vol_size):
        """Extend the volume from size src_size to size vol_size."""
        if vol_size > src_size:
            vol_size = vol_size * units.Gi
            self._array.extend_volume(vol_name, vol_size)

    def delete_volume(self, volume):
        """Disconnect all hosts and delete the volume"""
        LOG.debug("Enter PureISCSIDriver.delete_volume.")
        vol_name = _get_vol_name(volume)
        try:
            connected_hosts = self._array.list_volume_hosts(vol_name)
            for host_info in connected_hosts:
                host_name = host_info["host"]
                self._disconnect_host(host_name, vol_name)
            self._array.destroy_volume(vol_name)
        except exception.PureAPIException as err:
            with excutils.save_and_reraise_exception() as ctxt:
                if err.kwargs["code"] == 400 and \
                        "Volume does not exist" in err.msg:
                    # Happens if the volume does not exist.
                    ctxt.reraise = False
                    LOG.warn(_LW("Volume deletion failed with message: %s")
                             % err.msg)
        LOG.debug("Leave PureISCSIDriver.delete_volume.")

    def create_snapshot(self, snapshot):
        """Creates a snapshot."""
        LOG.debug("Enter PureISCSIDriver.create_snapshot.")
        vol_name, snap_suff = _get_snap_name(snapshot).split(".")
        self._array.create_snapshot(vol_name, snap_suff)
        LOG.debug("Leave PureISCSIDriver.create_snapshot.")

    def delete_snapshot(self, snapshot):
        """Deletes a snapshot."""
        LOG.debug("Enter PureISCSIDriver.delete_snapshot.")
        snap_name = _get_snap_name(snapshot)
        try:
            self._array.destroy_volume(snap_name)
        except exception.PureAPIException as err:
            with excutils.save_and_reraise_exception() as ctxt:
                if err.kwargs["code"] == 400:
                    # Happens if the snapshot does not exist.
                    ctxt.reraise = False
                    LOG.error(_LE("Snapshot deletion failed with message:"
                                  " %s") % err.msg)
        LOG.debug("Leave PureISCSIDriver.delete_snapshot.")

    def initialize_connection(self, volume, connector):
        """Allow connection to connector and return connection info."""
        LOG.debug("Enter PureISCSIDriver.initialize_connection.")
        target_port = self._get_target_iscsi_port()
        connection = self._connect(volume, connector)
        properties = {
            "driver_volume_type": "iscsi",
            "data": {
                "target_iqn": target_port["iqn"],
                "target_portal": target_port["portal"],
                "target_lun": connection["lun"],
                "target_discovered": True,
                "access_mode": "rw",
            },
        }
        LOG.debug("Leave PureISCSIDriver.initialize_connection. "
                  "Return value: " + str(properties))
        return properties

    def _get_target_iscsi_port(self):
        """Return dictionary describing iSCSI-enabled port on target array."""
        try:
            self._run_iscsiadm_bare(["-m", "discovery", "-t", "sendtargets",
                                     "-p", self._iscsi_port["portal"]])
        except processutils.ProcessExecutionError as err:
            LOG.warn(_LW("iSCSI discovery of port %(port_name)s at "
                         "%(port_portal)s failed with error: %(err_msg)s") %
                     {"port_name": self._iscsi_port["name"],
                      "port_portal": self._iscsi_port["portal"],
                      "err_msg": err.stderr})
            self._iscsi_port = self._choose_target_iscsi_port()
        return self._iscsi_port

    def _choose_target_iscsi_port(self):
        """Find a reachable iSCSI-enabled port on target array."""
        ports = self._array.list_ports()
        iscsi_ports = [port for port in ports if port["iqn"]]
        for port in iscsi_ports:
            try:
                self._run_iscsiadm_bare(["-m", "discovery",
                                         "-t", "sendtargets",
                                         "-p", port["portal"]])
            except processutils.ProcessExecutionError as err:
                LOG.debug(("iSCSI discovery of port %(port_name)s at "
                           "%(port_portal)s failed with error: %(err_msg)s") %
                          {"port_name": self._iscsi_port["name"],
                           "port_portal": self._iscsi_port["portal"],
                           "err_msg": err.stderr})
            else:
                LOG.info(_LI("Using port %(name)s on the array at %(portal)s "
                             "for iSCSI connectivity.") %
                         {"name": port["name"], "portal": port["portal"]})
                return port
        raise exception.PureDriverException(
            reason=_LE("No reachable iSCSI-enabled ports on target array."))

    def _connect(self, volume, connector):
        """Connect the host and volume; return dict describing connection."""
        connection = None
        vol_name = _get_vol_name(volume)
        host = self._get_host(connector)
        if host:
            host_name = host["name"]
            LOG.info(_LI("Re-using existing purity host %(host_name)r")
                     % {"host_name": host_name})
        else:
            host_name = _generate_purity_host_name(connector["host"])
            iqn = connector["initiator"]
            LOG.info(_LI("Creating host object %(host_name)r with IQN:"
                         " %(iqn)s.") % {"host_name": host_name, "iqn": iqn})
            self._array.create_host(host_name, iqnlist=[iqn])

        try:
            connection = self._array.connect_host(host_name, vol_name)
        except exception.PureAPIException as err:
            with excutils.save_and_reraise_exception() as ctxt:
                if (err.kwargs["code"] == 400 and
                        "Connection already exists" in err.msg):
                    # Happens if the volume is already connected to the host.
                    ctxt.reraise = False
                    LOG.warn(_LW("Volume connection already exists with "
                                 "message: %s") % err.msg)
                    # Get the info for the existing connection
                    connected_hosts = self._array.list_volume_hosts(vol_name)
                    for host_info in connected_hosts:
                        if host_info["host"] == host_name:
                            connection = host_info
                            break
        if not connection:
            raise exception.PureDriverException(
                reason=_("Unable to connect or find connection to host"))
        return connection

    def _get_host(self, connector):
        """Return dict describing existing Purity host object or None."""
        hosts = self._array.list_hosts()
        for host in hosts:
            if connector["initiator"] in host["iqn"]:
                return host
        return None

    def terminate_connection(self, volume, connector, **kwargs):
        """Terminate connection."""
        LOG.debug("Enter PureISCSIDriver.terminate_connection.")
        vol_name = _get_vol_name(volume)
        host = self._get_host(connector)
        if host:
            host_name = host["name"]
            self._disconnect_host(host_name, vol_name)
        else:
            LOG.error(_LE("Unable to find host object in Purity with IQN: "
                          "%(iqn)s.") % {"iqn": connector["initiator"]})
        LOG.debug("Leave PureISCSIDriver.terminate_connection.")

    def _disconnect_host(self, host_name, vol_name):
        LOG.debug("Enter PureISCSIDriver._disconnect_host.")
        try:
            self._array.disconnect_host(host_name, vol_name)
        except exception.PureAPIException as err:
            with excutils.save_and_reraise_exception() as ctxt:
                if err.kwargs["code"] == 400:
                    # Happens if the host and volume are not connected.
                    ctxt.reraise = False
                    LOG.error(_LE("Disconnection failed with message: "
                                  "%(msg)s.") % {"msg": err.msg})
        if (GENERATED_NAME.match(host_name) and
            not self._array.list_host_connections(host_name,
                                                  private=True)):
            LOG.info(_LI("Deleting unneeded host %(host_name)r.") %
                     {"host_name": host_name})
            self._array.delete_host(host_name)
        LOG.debug("Leave PureISCSIDriver._disconnect_host.")

    def get_volume_stats(self, refresh=False):
        """Return the current state of the volume service.

        If 'refresh' is True, run the update first.
        """

        LOG.debug("Enter PureISCSIDriver.get_volume_stats.")
        if refresh:
            LOG.debug("Updating volume stats.")
            self._update_stats()
        LOG.debug("Leave PureISCSIDriver.get_volume_stats.")
        return self._stats

    def _update_stats(self):
        """Set self._stats with relevant information."""
        info = self._array.get_array(space=True)
        total = float(info["capacity"]) / units.Gi
        free = float(info["capacity"] - info["total"]) / units.Gi
        data = {"volume_backend_name": self._backend_name,
                "vendor_name": "Pure Storage",
                "driver_version": self.VERSION,
                "storage_protocol": "iSCSI",
                "total_capacity_gb": total,
                "free_capacity_gb": free,
                "reserved_percentage": 0,
                }
        self._stats = data

    def extend_volume(self, volume, new_size):
        """Extend volume to new_size."""
        LOG.debug("Enter PureISCSIDriver.extend_volume.")
        vol_name = _get_vol_name(volume)
        new_size = new_size * units.Gi
        self._array.extend_volume(vol_name, new_size)
        LOG.debug("Leave PureISCSIDriver.extend_volume.")


class FlashArray(object):
    """Wrapper for Pure Storage REST API."""
    SUPPORTED_REST_API_VERSIONS = ["1.3", "1.2"]

    def __init__(self, target, api_token):
        cookie_handler = urllib2.HTTPCookieProcessor(cookielib.CookieJar())
        self._opener = urllib2.build_opener(cookie_handler)
        self._target = target
        self._rest_version = self._choose_rest_version()
        self._root_url = "https://%s/api/%s/" % (target, self._rest_version)
        self._api_token = api_token
        self._start_session()

    def _http_request(self, method, path, data=None, reestablish_session=True):
        """Perform HTTP request for REST API."""
        req = urllib2.Request(self._root_url + path,
                              headers={"Content-Type": "application/json"})
        req.get_method = lambda: method
        body = json.dumps(data)
        try:
            # Raises urllib2.HTTPError if response code != 200
            response = self._opener.open(req, body)
        except urllib2.HTTPError as err:
            if (reestablish_session and err.code == 401):
                self._start_session()
                return self._http_request(method, path, data,
                                          reestablish_session=False)
            elif err.code == 450:
                # Purity REST API version is bad
                new_version = self._choose_rest_version()
                if new_version == self._rest_version:
                    raise exception.PureAPIException(
                        code=err.code,
                        reason=(_LE("Unable to find usable REST API version. "
                                    "Response from Pure Storage REST API: ") +
                                err.read()))
                self._rest_version = new_version
                self._root_url = "https://%s/api/%s/" % (self._target,
                                                         self._rest_version)
                return self._http_request(method, path, data)
            else:
                raise exception.PureAPIException(code=err.code,
                                                 reason=err.read())
        except urllib2.URLError as err:
            # Error outside scope of HTTP status codes,
            # e.g., unable to resolve domain name
            raise exception.PureDriverException(
                reason=_LE("Unable to connect to %r. Check san_ip.") %
                self._target)
        else:
            content = response.read()
            if "application/json" in response.info().get('Content-Type'):
                return json.loads(content)
            raise exception.PureAPIException(
                reason=(_LE("Response not in JSON: ") + content))

    def _choose_rest_version(self):
        """Return a REST API version."""
        self._root_url = "https://%s/api/" % self._target
        data = self._http_request("GET", "api_version")
        available_versions = data["version"]
        available_versions.sort(reverse=True)
        for version in available_versions:
            if version in FlashArray.SUPPORTED_REST_API_VERSIONS:
                return version
        raise exception.PureDriverException(
            reason=_LE("All REST API versions supported by this version of "
                       "the Pure Storage iSCSI driver are unavailable on "
                       "array."))

    def _start_session(self):
        """Start a REST API session."""
        self._http_request("POST", "auth/session",
                           {"api_token": self._api_token},
                           reestablish_session=False)

    def get_array(self, **kwargs):
        """Return a dictionary containing information about the array."""
        return self._http_request("GET", "array", kwargs)

    def create_volume(self, name, size):
        """Create a volume and return a dictionary describing it."""
        return self._http_request("POST", "volume/%s" % name,
                                  {"size": size})

    def copy_volume(self, source, dest):
        """Clone a volume and return a dictionary describing the new volume."""
        return self._http_request("POST", "volume/%s" % dest,
                                  {"source": source})

    def create_snapshot(self, volume, suffix):
        """Create a snapshot and return a dictionary describing it."""
        data = {"source": [volume], "suffix": suffix, "snap": True}
        return self._http_request("POST", "volume", data)[0]

    def destroy_volume(self, volume):
        """Destroy an existing volume or snapshot."""
        return self._http_request("DELETE", "volume/%s" % volume)

    def extend_volume(self, volume, size):
        """Extend a volume to a new, larger size."""
        return self._http_request("PUT", "volume/%s" % volume,
                                  {"size": size, "truncate": False})

    def list_hosts(self, **kwargs):
        """Return a list of dictionaries describing each host."""
        return self._http_request("GET", "host", kwargs)

    def list_host_connections(self, host, **kwargs):
        """Return a list of dictionaries describing connected volumes."""
        return self._http_request("GET", "host/%s/volume" % host, kwargs)

    def create_host(self, host, **kwargs):
        """Create a host."""
        return self._http_request("POST", "host/%s" % host, kwargs)

    def delete_host(self, host):
        """Delete a host."""
        return self._http_request("DELETE", "host/%s" % host)

    def connect_host(self, host, volume, **kwargs):
        """Create a connection between a host and a volume."""
        return self._http_request("POST",
                                  "host/%s/volume/%s" % (host, volume),
                                  kwargs)

    def disconnect_host(self, host, volume):
        """Delete a connection between a host and a volume."""
        return self._http_request("DELETE",
                                  "host/%s/volume/%s" % (host, volume))

    def set_host(self, host, **kwargs):
        """Set an attribute of a host."""
        return self._http_request("PUT", "host/%s" % host, kwargs)

    def list_ports(self, **kwargs):
        """Return a list of dictionaries describing ports."""
        return self._http_request("GET", "port", kwargs)

    def list_volume_hosts(self, volume):
        """Return a list of dictionaries describing connected hosts."""
        return self._http_request("GET", "volume/%s/host" % volume)

    def create_pgroup(self, name):
        return self._http_request("POST", "pgroup/%s" % name)

    def delete_pgroup(self, name):
        return self._http_request("DELETE", "pgroup/%s" % name)

    def create_pgroup_snapshot(self, pgroup_name, pgsnapshot_suffix):
        params = {
            "snap": True,
            "suffix": pgsnapshot_suffix,
            "source": [pgroup_name]
        }
        return self._http_request("POST", "pgroup", params)

    def delete_pgroup_snapshot(self, name):
        return self._http_request("DELETE", "pgroup/%s" % name)

    def add_volume_to_pgroup(self, pgroup_name, volume_name):
        return self._http_request("PUT", "pgroup/%s" % pgroup_name,
                                  {"addvollist": [volume_name]})
