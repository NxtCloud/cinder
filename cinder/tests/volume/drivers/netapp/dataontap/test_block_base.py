# Copyright (c) 2014 Alex Meade.  All rights reserved.
# Copyright (c) 2014 Clinton Knight.  All rights reserved.
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
Mock unit tests for the NetApp block storage library
"""


import uuid

import mock

from cinder import exception
from cinder import test
from cinder.tests.volume.drivers.netapp.dataontap import fakes as fake
from cinder.volume.drivers.netapp.dataontap import block_base
from cinder.volume.drivers.netapp.dataontap.block_base import \
    NetAppBlockStorageLibrary as block_lib
from cinder.volume.drivers.netapp.dataontap.client.api import NaApiError
from cinder.volume.drivers.netapp import utils as na_utils


class NetAppBlockStorageLibraryTestCase(test.TestCase):

    def setUp(self):
        super(NetAppBlockStorageLibraryTestCase, self).setUp()

        kwargs = {'configuration': mock.Mock()}
        self.library = block_lib('driver', 'protocol', **kwargs)
        self.library.zapi_client = mock.Mock()
        self.zapi_client = self.library.zapi_client
        self.mock_request = mock.Mock()

    def tearDown(self):
        super(NetAppBlockStorageLibraryTestCase, self).tearDown()

    @mock.patch.object(block_lib, '_get_lun_attr',
                       mock.Mock(return_value={'Volume': 'vol1'}))
    def test_get_pool(self):
        pool = self.library.get_pool({'name': 'volume-fake-uuid'})
        self.assertEqual(pool, 'vol1')

    @mock.patch.object(block_lib, '_get_lun_attr',
                       mock.Mock(return_value=None))
    def test_get_pool_no_metadata(self):
        pool = self.library.get_pool({'name': 'volume-fake-uuid'})
        self.assertEqual(pool, None)

    @mock.patch.object(block_lib, '_get_lun_attr',
                       mock.Mock(return_value=dict()))
    def test_get_pool_volume_unknown(self):
        pool = self.library.get_pool({'name': 'volume-fake-uuid'})
        self.assertEqual(pool, None)

    @mock.patch.object(block_lib, '_create_lun', mock.Mock())
    @mock.patch.object(block_lib, '_create_lun_handle', mock.Mock())
    @mock.patch.object(block_lib, '_add_lun_to_table', mock.Mock())
    @mock.patch.object(na_utils, 'get_volume_extra_specs',
                       mock.Mock(return_value=None))
    @mock.patch.object(block_base, 'LOG', mock.Mock())
    def test_create_volume(self):
        self.library.create_volume({'name': 'lun1', 'size': 100,
                                    'id': uuid.uuid4(),
                                    'host': 'hostname@backend#vol1'})
        self.library._create_lun.assert_called_once_with(
            'vol1', 'lun1', 107374182400, mock.ANY, None)
        self.assertEqual(0, block_base.LOG.warning.call_count)

    def test_create_volume_no_pool_provided_by_scheduler(self):
        self.assertRaises(exception.InvalidHost, self.library.create_volume,
                          {'name': 'lun1', 'size': 100,
                           'id': uuid.uuid4(),
                           'host': 'hostname@backend'})  # missing pool

    @mock.patch.object(block_lib, '_get_lun_attr')
    @mock.patch.object(block_lib, '_get_or_create_igroup')
    def test_map_lun(self, mock_get_or_create_igroup, mock_get_lun_attr):
        os = 'linux'
        protocol = 'fcp'
        mock_get_lun_attr.return_value = {'Path': fake.LUN1, 'OsType': os}
        mock_get_or_create_igroup.return_value = fake.IGROUP1_NAME
        self.zapi_client.map_lun.return_value = '1'

        lun_id = self.library._map_lun('fake_volume',
                                       fake.FC_FORMATTED_INITIATORS,
                                       protocol, None)

        self.assertEqual(lun_id, '1')
        mock_get_or_create_igroup.assert_called_once_with(
            fake.FC_FORMATTED_INITIATORS, protocol, os)
        self.zapi_client.map_lun.assert_called_once_with(
            fake.LUN1, fake.IGROUP1_NAME, lun_id=None)

    @mock.patch.object(block_lib, '_get_lun_attr')
    @mock.patch.object(block_lib, '_get_or_create_igroup')
    @mock.patch.object(block_lib, '_find_mapped_lun_igroup')
    def test_map_lun_preexisting(self, mock_find_mapped_lun_igroup,
                                 mock_get_or_create_igroup, mock_get_lun_attr):
        os = 'linux'
        protocol = 'fcp'
        mock_get_lun_attr.return_value = {'Path': fake.LUN1, 'OsType': os}
        mock_get_or_create_igroup.return_value = fake.IGROUP1_NAME
        mock_find_mapped_lun_igroup.return_value = (fake.IGROUP1_NAME, '2')
        self.zapi_client.map_lun.side_effect = NaApiError

        lun_id = self.library._map_lun(
            'fake_volume', fake.FC_FORMATTED_INITIATORS, protocol, None)

        self.assertEqual(lun_id, '2')
        mock_find_mapped_lun_igroup.assert_called_once_with(
            fake.LUN1, fake.FC_FORMATTED_INITIATORS)

    @mock.patch.object(block_lib, '_get_lun_attr')
    @mock.patch.object(block_lib, '_get_or_create_igroup')
    @mock.patch.object(block_lib, '_find_mapped_lun_igroup')
    def test_map_lun_api_error(self, mock_find_mapped_lun_igroup,
                               mock_get_or_create_igroup, mock_get_lun_attr):
        os = 'linux'
        protocol = 'fcp'
        mock_get_lun_attr.return_value = {'Path': fake.LUN1, 'OsType': os}
        mock_get_or_create_igroup.return_value = fake.IGROUP1_NAME
        mock_find_mapped_lun_igroup.return_value = (None, None)
        self.zapi_client.map_lun.side_effect = NaApiError

        self.assertRaises(NaApiError, self.library._map_lun, 'fake_volume',
                          fake.FC_FORMATTED_INITIATORS, protocol, None)

    @mock.patch.object(block_lib, '_find_mapped_lun_igroup')
    def test_unmap_lun(self, mock_find_mapped_lun_igroup):
        mock_find_mapped_lun_igroup.return_value = (fake.IGROUP1_NAME, 1)

        self.library._unmap_lun(fake.LUN1, fake.FC_FORMATTED_INITIATORS)

        self.zapi_client.unmap_lun.assert_called_once_with(fake.LUN1,
                                                           fake.IGROUP1_NAME)

    def test_find_mapped_lun_igroup(self):
        self.assertRaises(NotImplementedError,
                          self.library._find_mapped_lun_igroup,
                          fake.LUN1,
                          fake.FC_FORMATTED_INITIATORS)

    def test_has_luns_mapped_to_initiators(self):
        self.zapi_client.has_luns_mapped_to_initiators.return_value = True
        self.assertTrue(self.library._has_luns_mapped_to_initiators(
            fake.FC_FORMATTED_INITIATORS))
        self.zapi_client.has_luns_mapped_to_initiators.assert_called_once_with(
            fake.FC_FORMATTED_INITIATORS)

    def test_get_or_create_igroup_preexisting(self):
        self.zapi_client.get_igroup_by_initiators.return_value = [fake.IGROUP1]

        igroup_name = self.library._get_or_create_igroup(
            fake.FC_FORMATTED_INITIATORS, 'fcp', 'linux')

        self.assertEqual(igroup_name, fake.IGROUP1_NAME)
        self.zapi_client.get_igroup_by_initiators.assert_called_once_with(
            fake.FC_FORMATTED_INITIATORS)

    @mock.patch.object(uuid, 'uuid4', mock.Mock(return_value=fake.UUID1))
    def test_get_or_create_igroup_none_preexisting(self):
        self.zapi_client.get_igroup_by_initiators.return_value = []

        igroup_name = self.library._get_or_create_igroup(
            fake.FC_FORMATTED_INITIATORS, 'fcp', 'linux')

        self.assertEqual(igroup_name, 'openstack-' + fake.UUID1)
        self.zapi_client.create_igroup.assert_called_once_with(
            igroup_name, 'fcp', 'linux')
        self.assertEqual(len(fake.FC_FORMATTED_INITIATORS),
                         self.zapi_client.add_igroup_initiator.call_count)

    def test_get_fc_target_wwpns(self):
        self.assertRaises(NotImplementedError,
                          self.library._get_fc_target_wwpns)

    @mock.patch.object(block_lib, '_build_initiator_target_map')
    @mock.patch.object(block_lib, '_map_lun')
    def test_initialize_connection_fc(self, mock_map_lun,
                                      mock_build_initiator_target_map):
        self.maxDiff = None
        mock_map_lun.return_value = '1'
        mock_build_initiator_target_map.return_value = (fake.FC_TARGET_WWPNS,
                                                        fake.FC_I_T_MAP, 4)

        target_info = self.library.initialize_connection_fc(fake.FC_VOLUME,
                                                            fake.FC_CONNECTOR)

        self.assertDictEqual(target_info, fake.FC_TARGET_INFO)
        mock_map_lun.assert_called_once_with(
            'fake_volume', fake.FC_FORMATTED_INITIATORS, 'fcp', None)

    @mock.patch.object(block_lib, '_build_initiator_target_map')
    @mock.patch.object(block_lib, '_map_lun')
    def test_initialize_connection_fc_no_wwpns(
            self, mock_map_lun, mock_build_initiator_target_map):

        mock_map_lun.return_value = '1'
        mock_build_initiator_target_map.return_value = (None, None, 0)
        self.assertRaises(exception.VolumeBackendAPIException,
                          self.library.initialize_connection_fc,
                          fake.FC_VOLUME,
                          fake.FC_CONNECTOR)

    @mock.patch.object(block_lib, '_has_luns_mapped_to_initiators')
    @mock.patch.object(block_lib, '_unmap_lun')
    @mock.patch.object(block_lib, '_get_lun_attr')
    def test_terminate_connection_fc(self, mock_get_lun_attr, mock_unmap_lun,
                                     mock_has_luns_mapped_to_initiators):

        mock_get_lun_attr.return_value = {'Path': fake.LUN1}
        mock_unmap_lun.return_value = None
        mock_has_luns_mapped_to_initiators.return_value = True

        target_info = self.library.terminate_connection_fc(fake.FC_VOLUME,
                                                           fake.FC_CONNECTOR)

        self.assertDictEqual(target_info, fake.FC_TARGET_INFO_EMPTY)
        mock_unmap_lun.assert_called_once_with(fake.LUN1,
                                               fake.FC_FORMATTED_INITIATORS)

    @mock.patch.object(block_lib, '_build_initiator_target_map')
    @mock.patch.object(block_lib, '_has_luns_mapped_to_initiators')
    @mock.patch.object(block_lib, '_unmap_lun')
    @mock.patch.object(block_lib, '_get_lun_attr')
    def test_terminate_connection_fc_no_more_luns(
            self, mock_get_lun_attr, mock_unmap_lun,
            mock_has_luns_mapped_to_initiators,
            mock_build_initiator_target_map):

        mock_get_lun_attr.return_value = {'Path': fake.LUN1}
        mock_unmap_lun.return_value = None
        mock_has_luns_mapped_to_initiators.return_value = False
        mock_build_initiator_target_map.return_value = (fake.FC_TARGET_WWPNS,
                                                        fake.FC_I_T_MAP, 4)

        target_info = self.library.terminate_connection_fc(fake.FC_VOLUME,
                                                           fake.FC_CONNECTOR)

        self.assertDictEqual(target_info, fake.FC_TARGET_INFO_UNMAP)

    @mock.patch.object(block_lib, '_get_fc_target_wwpns')
    def test_build_initiator_target_map_no_lookup_service(
            self, mock_get_fc_target_wwpns):

        self.library.lookup_service = None
        mock_get_fc_target_wwpns.return_value = fake.FC_FORMATTED_TARGET_WWPNS

        (target_wwpns, init_targ_map, num_paths) = \
            self.library._build_initiator_target_map(fake.FC_CONNECTOR)

        self.assertSetEqual(set(fake.FC_TARGET_WWPNS), set(target_wwpns))
        self.assertDictEqual(fake.FC_I_T_MAP_COMPLETE, init_targ_map)
        self.assertEqual(0, num_paths)

    @mock.patch.object(block_lib, '_get_fc_target_wwpns')
    def test_build_initiator_target_map_with_lookup_service(
            self, mock_get_fc_target_wwpns):

        self.library.lookup_service = mock.Mock()
        self.library.lookup_service.get_device_mapping_from_network.\
            return_value = fake.FC_FABRIC_MAP
        mock_get_fc_target_wwpns.return_value = fake.FC_FORMATTED_TARGET_WWPNS

        (target_wwpns, init_targ_map, num_paths) = \
            self.library._build_initiator_target_map(fake.FC_CONNECTOR)

        self.assertSetEqual(set(fake.FC_TARGET_WWPNS), set(target_wwpns))
        self.assertDictEqual(fake.FC_I_T_MAP, init_targ_map)
        self.assertEqual(4, num_paths)

    @mock.patch.object(block_lib, '_create_lun', mock.Mock())
    @mock.patch.object(block_lib, '_create_lun_handle', mock.Mock())
    @mock.patch.object(block_lib, '_add_lun_to_table', mock.Mock())
    @mock.patch.object(na_utils, 'LOG', mock.Mock())
    @mock.patch.object(na_utils, 'get_volume_extra_specs',
                       mock.Mock(return_value={'netapp:raid_type': 'raid4'}))
    def test_create_volume_obsolete_extra_spec(self):

        self.library.create_volume({'name': 'lun1', 'size': 100,
                                    'id': uuid.uuid4(),
                                    'host': 'hostname@backend#vol1'})

        warn_msg = 'Extra spec netapp:raid_type is obsolete.  ' \
                   'Use netapp_raid_type instead.'
        na_utils.LOG.warning.assert_called_once_with(warn_msg)

    @mock.patch.object(block_lib, '_create_lun', mock.Mock())
    @mock.patch.object(block_lib, '_create_lun_handle', mock.Mock())
    @mock.patch.object(block_lib, '_add_lun_to_table', mock.Mock())
    @mock.patch.object(na_utils, 'LOG', mock.Mock())
    @mock.patch.object(na_utils, 'get_volume_extra_specs',
                       mock.Mock(return_value={'netapp_thick_provisioned':
                                               'true'}))
    def test_create_volume_deprecated_extra_spec(self):

        self.library.create_volume({'name': 'lun1', 'size': 100,
                                    'id': uuid.uuid4(),
                                    'host': 'hostname@backend#vol1'})

        warn_msg = 'Extra spec netapp_thick_provisioned is deprecated.  ' \
                   'Use netapp_thin_provisioned instead.'
        na_utils.LOG.warning.assert_called_once_with(warn_msg)
