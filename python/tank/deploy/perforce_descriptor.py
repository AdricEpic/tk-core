# Copyright 1998-2016 Epic Games, Inc. All Rights Reserved.

"""
This Perforce descriptor is for a Perforce-based workflow.
It will base version numbering off labels in Perforce.
"""

import copy
import os
import P4
import re
import tempfile
import uuid

from .util import subprocess_check_output
from ..api import Tank
from ..errors import TankError
from ..platform import constants
from .descriptor import AppDescriptor, VersionedSingletonDescriptor
from .zipfilehelper import unzip_file


class TankPerforceDescriptor(VersionedSingletonDescriptor):
    """
    Represents a path spec in Perforce. New versions are represented by new labels.

    path must be on the form:
    server:port//path/to/app...
        eg. perforce:1666//depot/Shotgun/myApp/...
    """

    # ToDo: Can assume trailing /..., since we'll always want everything below?

    def __init__(self, pc_path, bundle_install_path, location_dict, app_type):
        super(TankPerforceDescriptor, self).__init__(pc_path, bundle_install_path, location_dict)

        self._type = app_type
        # ToDo: Split path descriptor into components
        self._path = location_dict.get("path")
        # strip trailing slashes - this is so that when we build
        # the name later (using os.basename) we construct it correctly.
        if self._path.endswith("/") or self._path.endswith("\\"):
            self._path = self._path[:-1]
        self._version = location_dict.get("version")

        if self._path is None or self._version is None:
            raise TankError("Perforce descriptor is not valid: %s" % str(location_dict))

    def get_system_name(self):
        """
        Returns a short name, suitable for use in configuration files
        and for folders on disk
        """
        bn = os.path.basename(self._path)
        (name, ext) = os.path.splitext(bn)
        return name

    def get_version(self):
        """
        Returns the version number string for this item
        """
        return self._version

    def get_path(self):
        """
        returns the path to the folder where this item resides
        """
        # perforce:1666//depot/shotgun/tk-myApp/... -> tk-myApp
        name = os.path.basename(self._path)
        return self._get_local_location(self._type, "perforce", name, self._version)

    def exists_local(self):
        """
        Returns true if this item exists in a local repo
        """
        return os.path.exists(self.get_path())

    def download_local(self):
        """
        Retrieves this version to local repo.
        Will exit early if app already exists local.
        """
        if self.exists_local():
            # nothing to do!
            return

        target = self.get_path()
        if not os.path.exists(target):
            old_umask = os.umask(0)
            os.makedirs(target, 0777)
            os.umask(old_umask)

        # Download files from the depot at the desired tag target location
        self.__sync_from_perforce(target)

    def find_latest_version(self, constraint_pattern=None):
        """
        Returns a descriptor object that represents the latest version.

        :param constraint_pattern: If this is specified, the query will be constrained
        by the given pattern. Version patterns are on the following forms:

            - v1.2.3 (means the descriptor returned will inevitably be same as self)
            - v1.2.x
            - v1.x.x

        :returns: descriptor object
        """
        if constraint_pattern:
            return self._find_latest_by_pattern(constraint_pattern)
        else:
            return self._find_latest_version()

    def _find_latest_by_pattern(self, pattern):
        """
        Returns a descriptor object that represents the latest
        version, but based on a version pattern.

        :param pattern: Version patterns are on the following forms:

            - v1.2.3 (can return this v1.2.3 but also any forked version under, eg. v1.2.3.2)
            - v1.2.x (examples: v1.2.4, or a forked version v1.2.4.2)
            - v1.x.x (examples: v1.3.2, a forked version v1.3.2.2)
            - v1.2.3.x (will always return a forked version, eg. v1.2.3.2)

        :returns: descriptor object
        """

        raise NotImplementedError

    def _find_latest_tag_by_pattern(self, version_numbers, pattern):
        """
        Given a list of version strings (e.g. 'v1.2.3'), find the one that best matches the given pattern.

        Version numbers passed in that don't match the pattern v1.2.3... will be ignored.

        :param version_numbers: List of version number strings, e.g. ``['v1.2.3', 'v1.2.5']``
        :param pattern: Version pattern string, e.g. 'v1.x.x'. Patterns are on the following forms:

            - v1.2.3 (can return this v1.2.3 but also any forked version under, eg. v1.2.3.2)
            - v1.2.x (examples: v1.2.4, or a forked version v1.2.4.2)
            - v1.x.x (examples: v1.3.2, a forked version v1.3.2.2)
            - v1.2.3.x (will always return a forked version, eg. v1.2.3.2)

        :returns: The most appropriate tag in the given list of tags
        :raises: TankError if parsing fails
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
            except Exception:
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
        # version_to_use = None

        raise NotImplementedError

    def _find_latest_version(self):
        """
        Returns a descriptor object that represents the latest version.

        :returns: descriptor object
        """
        raise NotImplementedError

    def __sync_from_perforce(self, target_path):
        """
        Syncs temp workspace to label matching desired version.

        :raises: P4Exception if sync fails
        """
        # ToDo: Add version tag parameter to P4 commands

        p4 = P4.P4()
        p4.connect()
        sanitized_depot_path = self._path.rstrip('./')
        src_files = [i['depotFile'] for i in p4.run_files(sanitized_depot_path + '/...')]
        for src_fpath in src_files:
            dest_fpath = os.path.join(target_path, os.path.relpath(src_fpath, sanitized_depot_path))
            p4.run_print('-o', dest_fpath, src_fpath)

        # ToDo: change file permissions from read-only
