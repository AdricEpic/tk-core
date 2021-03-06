# Copyright (c) 2016 Shotgun Software Inc.
# 
# CONFIDENTIAL AND PROPRIETARY
# 
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit 
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your 
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights 
# not expressly granted therein are reserved by Shotgun Software Inc.

import os
import re
import cgi
import sys
import urlparse

from .. import constants
from ... import LogManager
from ...util import filesystem
from ..errors import TankDescriptorError

from tank_vendor import yaml

log = LogManager.get_logger(__name__)



class IODescriptorBase(object):
    """
    An I/O descriptor describes a particular version of an app, engine or core component.
    It also knows how to access metadata such as documentation, descriptions etc.

    Several Descriptor classes exists, all deriving from this base class, and the
    factory method create_descriptor() manufactures the correct descriptor object
    based on a descriptor dict, that is found inside of the environment config.

    Different App Descriptor implementations typically handle different source control
    systems: There may be an app descriptor which knows how to communicate with the
    Tank App store and one which knows how to handle the local file system.
    """
    def __init__(self, descriptor_dict):
        """
        Constructor

        :param descriptor_dict: Dictionary describing what
                                the descriptor is pointing at
        """
        self._bundle_cache_root = None
        self._fallback_roots = []
        self._descriptor_dict = descriptor_dict
        self.__manifest_data = None

    def set_cache_roots(self, primary_root, fallback_roots):
        """
        Specify where to go look for cached versions of the app.
        The primary root is where new data is always written to
        if something is downloaded and cached. The fallback_roots
        parameter is a list of paths where the descriptor system
        will look in case a cached entry is not found in the
        primary root. If you specify several fallback roots, they
        will be traversed in order.

        This is an internal method that is part of the construction
        of the descriptor instances. Do not call directly.

        :param primary_root: Path for reading and writing cached apps
        :param fallback_roots: Paths to attempt to read cached apps from
                               in case it's not found in the primary root.
                               Paths will be traversed in the order they are
                               specified.

        """
        self._bundle_cache_root = primary_root
        self._fallback_roots = fallback_roots

    def __repr__(self):
        """
        Low level representation
        """
        class_name = self.__class__.__name__
        return "<%s %s>" % (class_name, self.get_uri())

    @classmethod
    def _validate_descriptor(cls, descriptor_dict, required, optional):
        """
        Validate that the descriptor dictionary has got the necessary keys.

        Raises TankDescriptorError if required parameters are missing.
        Logs warnings if parameters outside the required/optional range are specified.

        :param descriptor_dict: descriptor dict
        :param required: List of required parameters
        :param optional: List of optionally supported parameters
        :raises: TankDescriptorError if the descriptor dict does not include all parameters.
        """
        desc_keys_set = set(descriptor_dict.keys())
        required_set = set(required)
        optional_set = set(optional)

        if not required_set.issubset(desc_keys_set):
            missing_keys = required_set.difference(desc_keys_set)
            raise TankDescriptorError("%s are missing required keys %s" % (descriptor_dict, missing_keys))

        all_keys = required_set.union(optional_set)

        if desc_keys_set.difference(all_keys):
            log.warning(
                "Found unsupported parameters %s in %s. "
                "These will be ignored." % (desc_keys_set.difference(all_keys), descriptor_dict)
            )

    @classmethod
    def _get_legacy_bundle_install_folder(
        cls,
        descriptor_name,
        install_cache_root,
        bundle_type,
        bundle_name,
        bundle_version
    ):
        """Return the path to the legacy bundle install dir for the supplied info.

        :param descriptor_name: The name of the descriptor. ex: "app_store" or "git"
        :param install_cache_root: The root path to the bundle cache.
        :param bundle_type: The type of the bundle. Should be one of:
            Descriptor.APP, Descriptor.ENGINE, or Descriptor.FRAMEWORK.
        :param bundle_name: The display name for the resolved descriptor resource.
            ex: "tk-multi-shotgunpanel"
        :param bundle_version: The version of the bundle on disk. ex: "v1.2.5"
        :rtype: str
        :return: The path to the cache in the legacy bundle structure.
        :raises: RuntimeError - if the bundle_type is not recognized.

        This method is provided for compatibility with older versions of core,
        prior to v0.18.x. As of v0.18.x, the bundle cache subdirectory names
        were shortened and otherwise modified to help prevent MAX_PATH issues
        on windows. This method is used to add the old style path as a fallback
        for cases like core having been upgraded to v0.18.x on an existing project.

        New style cache path:
            <root>/app_store/tk-multi-shotgunpanel/v1.2.5

        Legacy style cache path:
            <root>/apps/app_store/tk-multi-shotgunpanel/v1.2.5

        For reference, this method emulates: `tank.deploy.descriptor._get_local_location`
        in the pre-v0.18.x core.

        """
        from ..descriptor import Descriptor

        if bundle_type == Descriptor.APP:
            legacy_dir = "apps"
        elif bundle_type == Descriptor.ENGINE:
            legacy_dir = "engines"
        elif bundle_type == Descriptor.FRAMEWORK:
            legacy_dir = "frameworks"
        else:
            raise RuntimeError(
                "Unknown bundle type '%s'. Can not determine legacy cache path." %
                (bundle_type,)
            )

        # build and return the path.
        # example: <root>/apps/app_store/tk-multi-shotgunpanel/v1.2.5
        return os.path.join(
            install_cache_root,
            legacy_dir,
            descriptor_name,
            bundle_name,
            bundle_version,
        )

    def _find_latest_tag_by_pattern(self, version_numbers, pattern):
        """
        Given a list of version strings (e.g. 'v1.2.3'), find the one
        that best matches the given pattern.

        Version numbers passed in that don't match the pattern v1.2.3... will be ignored.

        :param version_numbers: List of version number strings, e.g. ``['v1.2.3', 'v1.2.5']``
        :param pattern: Version pattern string, e.g. 'v1.x.x'. Patterns are on the following forms:

            - v1.2.3 (can return this v1.2.3 but also any forked version under, eg. v1.2.3.2)
            - v1.2.x (examples: v1.2.4, or a forked version v1.2.4.2)
            - v1.x.x (examples: v1.3.2, a forked version v1.3.2.2)
            - v1.2.3.x (will always return a forked version, eg. v1.2.3.2)

        :returns: The most appropriate tag in the given list of tags
        :raises: TankDescriptorError if parsing fails
        """
        # now put all version number strings which match the form
        # vX.Y.Z(.*) into a nested dictionary where it is keyed recursively
        # by each digit (ie. major, minor, increment, then any additional
        # digit optionally used by forked versions)
        #
        versions = {}
        for version_num in version_numbers:
            try:
                version_split = map(int, version_num[1:].split("."))
            except Exception, e:
                # this git tag is not on the expected form vX.Y.Z where X Y and Z are ints. skip.
                continue

            if len(version_split) < 3:
                # git tag has no minor or increment number. skip.
                continue

            # fill our versions dictionary
            #
            # For example, the following versions:
            # v1.2.1, v1.2.2, v1.2.3.1, v1.4.3, v1.4.2.1, v1.4.2.2, v1.4.1,
            #
            # Would generate the following:
            # {1:
            #   {2: {1: {},
            #        2: {},
            #        3: {1: {}
            #       }
            #   },
            #   4: {1: {},
            #       2: {1: {}, 2: {}},
            #       3: {}
            #       }
            #   }
            # }
            #
            current = versions
            for number in version_split:
                if number not in current:
                    current[number] = {}
                current = current[number]

        # now search for the latest version matching our pattern
        version_to_use = None
        if not re.match("^v([0-9]+|x)(.([0-9]+|x)){2,}$", pattern):
            raise TankDescriptorError("Cannot parse version expression '%s'!" % pattern)

        # split our pattern, beware each part is a string (even integers)
        version_split = re.findall("([0-9]+|x)", pattern)
        if 'x' in version_split:
            # check that we don't have an incorrect pattern using x
            # then a digit, eg. v4.x.2
            if re.match("^v[0-9\.]+[x\.]+[0-9\.]+$", pattern):
                raise TankDescriptorError(
                    "Incorrect version pattern '%s'. "
                    "There should be no digit after a 'x'." % pattern
                )

        current = versions
        version_to_use = None
        # process each digit in the pattern
        for version_digit in version_split:
            if version_digit == 'x':
                # replace the 'x' by the latest at this level
                version_digit = max(current.keys(), key=int)
            version_digit = int(version_digit)
            if version_digit not in current:
                raise TankDescriptorError(
                    "'%s' does not have a version matching the pattern '%s'. "
                    "Available versions are: %s" % (self.get_system_name(), pattern, ", ".join(version_numbers))
                )
            current = current[version_digit]
            if version_to_use is None:
                version_to_use = "v%d" % version_digit
            else:
                version_to_use = version_to_use + ".%d" % version_digit

        # at this point we have a matching version (eg. v4.x.x => v4.0.2) but
        # there may be forked versions under this 4.0.2, so continue to recurse into
        # the versions dictionary to find the latest forked version
        while len(current):
            version_digit = max(current.keys())
            current = current[version_digit]
            version_to_use = version_to_use + ".%d" % version_digit

        return version_to_use

    def copy(self, target_path, connected=False):
        """
        Copy the contents of the descriptor to an external location

        :param target_path: target path to copy the descriptor to.
        :param connected: For descriptor types that supports it, attempt
                          to create a 'connected' copy that has a relationship
                          with the descriptor. This is typically useful for SCMs
                          such as git, where rather than copying the content in
                          its raw form, you clone the repository, thereby creating
                          a setup where changes can be made and pushed back to the
                          connected server side repository.
        """
        log.debug("Copying %r -> %s" % (self, target_path))
        # base class implementation does a straight copy
        # make sure config exists
        self.ensure_local()
        # copy descriptor in
        filesystem.copy_folder(self.get_path(), target_path)

    def get_manifest(self):
        """
        Returns the info.yml metadata associated with this descriptor.
        Note that this call involves deep introspection; in order to
        access the metadata we normally need to have the code content
        local, so this method may trigger a remote code fetch if necessary.

        :returns: dictionary with the contents of info.yml
        """
        if self.__manifest_data is None:
            # make sure payload exists locally
            if not self.exists_local():
                # @todo - at this point add to a metadata cache for performance
                # we can either just store it in a pickle, in order to avoid yaml parsing, which
                # is expensive, or if we want to be more fancy, we can maintain a single
                # "registry" file which holds the metadata for all known bundles in a single place.
                # given that all descriptors are immutable (except the ones where the immutable)
                # property returns false, we can keep adding to this global cache file over time.
                self.download_local()

            # get the metadata
            bundle_root = self.get_path()
            file_path = os.path.join(bundle_root, constants.BUNDLE_METADATA_FILE)

            if not os.path.exists(file_path):
                # at this point we have downloaded the bundle, but it may have
                # an invalid internal structure.
                raise TankDescriptorError("Toolkit metadata file '%s' missing." % file_path)

            try:
                file_data = open(file_path)
                try:
                    metadata = yaml.load(file_data)
                finally:
                    file_data.close()
            except Exception, exp:
                raise TankDescriptorError("Cannot load metadata file '%s'. Error: %s" % (file_path, exp))

            # cache it
            self.__manifest_data = metadata

        return self.__manifest_data

    @classmethod
    def dict_from_uri(cls, uri):
        """
        Convert a uri string into a descriptor dictionary.

        Example:

        - uri:           sgtk:descriptor:app_store?name=hello&version=v123
        - expected_type: app_store
        - returns:   {'type': 'app_store',
                      'name': 'hello',
                      'version': 'v123'}

        :param uri: uri string
        :return: dictionary with keys type and all keys specified
                 in the item_keys parameter matched up by items in the
                 uri string.
        """
        parsed_uri = urlparse.urlparse(uri)

        # example:
        #
        # >>> urlparse.urlparse("sgtk:descriptor:app_store?foo=bar&baz=buz")
        #
        # ParseResult(scheme='sgtk', netloc='', path='descriptor:app_store',
        #             params='', query='foo=bar&baz=buz', fragment='')
        #
        #
        # NOTE - it seems on some versions of python the result is different.
        #        this includes python2.5 but seems to affect other SKUs as well.
        #
        # uri: sgtk:descriptor:app_store?version=v0.1.2&name=tk-bundle
        #
        # python 2.6+ expected: ParseResult(
        # scheme='sgtk',
        # netloc='',
        # path='descriptor:app_store',
        # params='',
        # query='version=v0.1.2&name=tk-bundle',
        # fragment='')
        #
        # python 2.5 and others: (
        # 'sgtk',
        # '',
        # 'descriptor:app_store?version=v0.1.2&name=tk-bundle',
        # '',
        # '',
        # '')

        if parsed_uri.scheme != constants.DESCRIPTOR_URI_PATH_SCHEME:
            raise TankDescriptorError("Invalid uri '%s' - must begin with 'sgtk'" % uri)

        if parsed_uri.query == "":
            # in python 2.5 and others, the querystring is part of the path (see above)
            (path, query) = parsed_uri.path.split("?")
        else:
            path = parsed_uri.path
            query = parsed_uri.query


        split_path = path.split(constants.DESCRIPTOR_URI_SEPARATOR)
        # e.g. 'descriptor:app_store' -> ('descriptor', 'app_store')
        if len(split_path) != 2 or split_path[0] != constants.DESCRIPTOR_URI_PATH_PREFIX:
            raise TankDescriptorError("Invalid uri '%s' - must begin with sgtk:descriptor" % uri)

        descriptor_dict = {}

        descriptor_dict["type"] = split_path[1]

        # now pop remaining keys into a dict and key by item_keys
        # note: using deprecated cfg method for 2.5 compatibility
        # example:
        # >>> cgi.parse_qs("path=foo&version=v1.2.3")
        # {'path': ['foo'], 'version': ['v1.2.3']}
        for (param, value) in cgi.parse_qs(query).iteritems():
            if len(value) > 1:
                raise TankDescriptorError("Invalid uri '%s' - duplicate parameters" % uri)
            descriptor_dict[param] = value[0]

        return descriptor_dict

    def get_dict(self):
        """
        Returns the dictionary associated with this descriptor
        """
        return self._descriptor_dict

    @classmethod
    def uri_from_dict(cls, descriptor_dict):
        """
        Create a descriptor uri given some data

        {'type': 'app_store', 'bar':'baz'} --> 'sgtk:descriptor:app_store?bar=baz'

        :param descriptor_dict: descriptor dictionary
        :return: descriptor uri
        """
        if "type" not in descriptor_dict:
            raise TankDescriptorError(
                "Cannot create uri from %s - missing type field" % descriptor_dict
            )

        uri_chunks = [
            constants.DESCRIPTOR_URI_PATH_SCHEME,
            constants.DESCRIPTOR_URI_PATH_PREFIX,
            descriptor_dict["type"]
        ]
        uri = constants.DESCRIPTOR_URI_SEPARATOR.join(uri_chunks)

        qs_chunks = []
        for (param, value) in descriptor_dict.iteritems():
            if param == "type":
                continue
            qs_chunks.append("%s=%s" % (param, value))
        qs = "&".join(qs_chunks)

        return "%s?%s" % (uri, qs)

    def get_uri(self):
        """
        Return the string based uri representation of this object

        :return: Uri string
        """
        return self.uri_from_dict(self._descriptor_dict)

    def get_deprecation_status(self):
        """
        Returns information about deprecation.

        :returns: Returns a tuple (is_deprecated, message) to indicate
                  if this item is deprecated.
        """
        # only some descriptors handle this. Default is to not support deprecation, e.g.
        # always return that things are active.
        return False, ""

    def get_changelog(self):
        """
        Returns information about the changelog for this item.

        :returns: A tuple (changelog_summary, changelog_url). Values may be None
                  to indicate that no changelog exists.
        """
        return (None, None)

    def is_dev(self):
        """
        Returns true if this item is intended for development purposes
        """
        return False

    def is_immutable(self):
        """
        Returns true if this item's content never changes
        """
        return True

    def ensure_local(self):
        """
        Convenience method. Ensures that the descriptor exists locally.
        """
        if not self.exists_local():
            log.debug("Downloading %s to the local Toolkit install location..." % self)
            self.download_local()

    def exists_local(self):
        """
        Returns true if this item exists in a locally accessible form
        """
        return self.get_path() is not None

    def get_path(self):
        """
        Returns the path to the folder where this item resides. If no
        cache exists for this path, None is returned.
        """
        for path in self._get_cache_paths():
            # we determine local existence based on the info.yml
            info_yml_path = os.path.join(path, constants.BUNDLE_METADATA_FILE)
            if os.path.exists(info_yml_path):
                return path

        return None


    ###############################################################################################
    # stuff typically implemented by deriving classes

    def _get_cache_paths(self):
        """
        Get a list of resolved paths, starting with the primary and
        continuing with alternative locations where it may reside

        Note: This method only computes paths and does not perform any I/O ops.

        :return: List of path strings
        """
        raise NotImplementedError

    def get_system_name(self):
        """
        Returns a short name, suitable for use in configuration files
        and for folders on disk, e.g. 'tk-maya'
        """
        raise NotImplementedError

    def get_version(self):
        """
        Returns the version number string for this item, .e.g 'v1.2.3'
        """
        raise NotImplementedError

    def download_local(self):
        """
        Retrieves this version to local repo.
        """
        raise NotImplementedError

    def get_latest_version(self, constraint_pattern=None):
        """
        Returns a descriptor object that represents the latest version.

        :param constraint_pattern: If this is specified, the query will be constrained
               by the given pattern. Version patterns are on the following forms:

                - v0.1.2, v0.12.3.2, v0.1.3beta - a specific version
                - v0.12.x - get the highest v0.12 version
                - v1.x.x - get the highest v1 version

        :returns: instance deriving from IODescriptorBase
        """
        raise NotImplementedError
