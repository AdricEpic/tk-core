# Copyright (c) 2016 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

import imp
import os
import sys
from threading import Lock


class CoreImportHandler(object):
    """A custom import handler to allow for core version switching.

    Usage:
        >>> import sys
        >>> from tank.shotgun_deploy import CoreImportHandler
        >>> importer = CoreImportHandler(
        >>>     ["sgtk", "tank", "tank_vendor"],
        >>>     "/path/to/a/version/of/tk-core",
        >>>     logger,
        >>> )
        >>> sys.meta_path.append(importer)
        >>> # import/run a bunch of code
        >>> # context change, need to use a different core
        >>> importer.set_core_path("/path/to/a/different/version/of/tk-core")
        >>> # namespaces cleared out and re-imported from new core location
        >>> # new imports will come from new core location

    When an instance of this object is added to `sys.meta_path`, it is used to
    alter the way python imports packages. The supplied `namespaces` limit the
    packages that the importer will operate on.

    The core path is used to locate modules attempting to be loaded. The core
    path can be set via `set_core_path` to alter the location of existing and
    future core imports.

    For more information on custom import hooks, see PEP 302:
        https://www.python.org/dev/peps/pep-0302/

    """

    def __init__(self, namespaces, core_path, logger):
        """Initialize the custom importer.

        :param namespaces: A list of core namespaces for limiting the custom
            behavior.
        :param core_path: A str path to the core location to import from.
        :param logger: A logger object

        """

        self._core_path = core_path
        self._log = logger

        # list of namespaces to operate on
        self._namespaces = namespaces

        # re-imports any existing modules matching the namespaces
        self.set_core_path(core_path)

        # used to lock while operating on sys.modules
        self._core_change_lock = Lock()

        # a dictionary to hold module information after it is found, before
        # it is loaded.
        self._module_info = {}

    def __repr__(self):
        return (
            "<CoreImportHandler with namespaces '%s' and core_path '%s'" %
            (self.namespaces, self.core_path)
        )

    def find_module(self, module_fullname, package_path=None):
        """Locates the given module in the current core.

        :param module_fullname: The fullname of the module to import
        :param package_path: None for a top-level module, or
            package.__path__ for submodules or subpackages

        The package_path is currently ignored by this method as it ensures we're
        importing the module from the current core path.

        For further info, see the docs on find_module here:
            https://docs.python.org/2/library/imp.html#imp.find_module

        :returns: this object (also a loader) if module found, None otherwise.
        """

        # get the package name from (first part of the module fullname)
        module_path_parts = module_fullname.split('.')
        package_name = module_path_parts[0]

        # make sure the package is in the list of supplied namespaces before
        # continuing.
        if package_name not in self._namespaces:
            # the package is not in one of the supplied namespaces, returning
            # None tells python to use the next importer available (likely the
            # default import mechanism).
            return None

        # the name of the module (without the module path)
        module_name = module_path_parts.pop()

        # the module path as an actual partial path on disk
        if len(module_path_parts):
            module_path = os.path.join(*module_path_parts)
        else:
            module_path = ""

        path = os.path.join(self.core_path, module_path)

        try:
            # find the module and store its info in a lookup based on the
            # full module name. The module info is a tuple of the form:
            #   (file_obj, filename, description)
            # If this find is successful, we'll need the info in order
            # to load it later.
            module_info = imp.find_module(module_name, [path])
            self._module_info[module_fullname] = module_info
        except ImportError:
            # no module found, fall back to regular import
            return None

        self._log.debug(
            "Custom core import of '%s' from '%s'" % (module_name, path))

        # since this object is also the "loader" return itself
        return self

    # -------------------------------------------------------------------------
    def load_module(self, module_fullname):
        """Custom loader.

        Called by python if the find_module was successful.

        For further info, see the docs on `load_module` here:
            https://docs.python.org/2/library/imp.html#imp.load_module

        :param module_fullname: The fullname of the module to import

        :returns: The loaded module object.

        """

        file_obj = None
        try:
            # retrieve the found module info
            (file_obj, filename, desc) = self._module_info[module_fullname]

            # attempt to load the module given the info from find_module
            module = sys.modules.setdefault(
                module_fullname,
                imp.load_module(module_fullname, file_obj, filename, desc)
            )
        finally:
            # as noted in the imp.load_module docs, must close the file handle.
            if file_obj:
                file_obj.close()

            # no need to carry around the module info now that we've loaded it
            del self._module_info[module_fullname]

        # the module needs to know the loader so that reload() works
        module.__loader__ = self

        # the module has been loaded from the proper core location!
        return module

    def set_core_path(self, path):
        """Set the core path to use.

        This method clears out `sys.modules` of all previously imported modules
        matching `self.namespaces` and reimports them using the new core path.
        This method locks the global interpreter in an attempt to prevent
        problems from modifying sys.modules in a multithreaded context.

        :param path: str path to the core to import from.

        :raises: ValueError - if the supplied path does not exist or is not
            a valid directory.
        """

        # the paths are the same. No need to do anything.
        if path == self.core_path:
            return

        if not os.path.exists(path):
            raise ValueError(
                "The supplied core path is not a valid directory: '%s'."
                % (path,)
            )

        # TODO: ensure that the directory looks like core?

        # set the core path internally. now that this is set,
        self._core_path = path

        # acquire a lock to prevent issues with other threads importing at the
        # same time.
        self._core_change_lock.acquire()

        # keep a runnng list of modules that need to be re-imported from the new
        # core location.
        modules_to_import = []

        for module in sys.modules.keys():

            # extract just the package name to see if it matches a namespace
            package_name = module.split(".")[0]

            if package_name in self.namespaces:
                # module is in one of the namespaces. remember the name so we
                # can re-import it. but first, delete it from sys.modules so
                # that the custom import can run.
                modules_to_import.append(module)
                del sys.modules[module]

        # now go through the list of previously imported modules and re-import
        # them. these imports will run through the custom `find_module` and
        # `load_module` and be imported from the newly set core path.
        for module in modules_to_import:
            try:
                __import__(module)
            except ImportError:
                # The existing module could not be re-imported. It may not be
                # necessary with the new core, so just ignore it.
                # Future attempts to import or use this module may raise, but
                # those exceptions will likely be more useful if raised in
                # context.
                pass

        # release the lock so that other threads can continue importing from
        # the new core location.
        self._core_change_lock.release()

    @property
    def core_path(self):
        """The core_path for this importer.

        :returns: str path to the core being used for imports
        """
        return self._core_path

    @property
    def namespaces(self):
        """The namespaces this importer operates on.

        :returns: a list where each item is a namespace str
        """
        return self._namespaces
