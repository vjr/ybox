"""
Classes and methods for bookkeeping the state of ybox containers including the packages
installed on each container explicitly.
"""

import json
import os
import re
import sqlite3
import typing
from configparser import ConfigParser
from contextlib import closing
from dataclasses import dataclass
from enum import Enum, IntFlag, auto
from importlib.resources import files
from io import StringIO
from pathlib import Path
from typing import Optional, Tuple, Union
from uuid import uuid4

from packaging.version import parse as parse_version

import ybox
from .env import Environ, PathName
from .print import print_warn


@dataclass
class RuntimeConfiguration:
    """
    Holds runtime configuration details of a container.

    Attributes:
        name: name of the container
        distribution: the Linux distribution used when creating the container
        shared_root: the local shared root directory if `shared_root` flag is enabled for
                     the container (see `shared_root` key in ybox/conf/profiles/basic.ini)
        ini_config: the resolved configuration of the container in INI format as a string or
                    a `ConfigParser` object
    """
    name: str
    distribution: str
    shared_root: str
    ini_config: Union[str, ConfigParser]


class CopyType(IntFlag):
    """
    Different types of local wrappers created for container desktop/executable files which
    is used in the `local_copy_type` field of the `packages` table.
    """
    DESKTOP = auto()
    EXECUTABLE = auto()


class DependencyType(str, Enum):
    """
    Different types of package dependencies. Used in `dep_type` field of the `package_deps` table.
    """
    REQUIRED = "required"
    OPTIONAL = "optional"
    SUGGESTION = "suggestion"


class YboxStateManagement:
    """
    Maintain the state of all ybox containers. This includes:

    1. The full configuration used for the creation of a container.
    2. The packages installed explicitly on each of the containers (though all
         packages may be visible on all containers having `shared_root` enabled)
    3. Cleanup state of containers removed explicitly or those that got stopped/removed.

    Expected usage is using a `with` statement to ensure proper cleanup other the database
    may be left in a locked state.

    The latest schema is now maintained as a SQL file `init.sql` in `ybox.schema` package.
    This is executed using `executescript` method of `sqlite3.Cursor`, so it supports normal
    multi-line SQL. In addition, support for including other files using `SOURCE '...'` has been
    provided as described below.

    Migration scripts: the code supports schema evolution using SQL migration scripts
    in `ybox.schema.migrate` package. The file name must follow `<old version>:<new version>.sql`
    naming convention (e.g. `0.9.0:0.9.1.sql`). The scripts are sorted by <old version> and
    executed, so at most one script for each version is expected. Version numbers follow
    the standard python convention, so there can be any number of alpha/beta/dev releases
    (see https://packaging.python.org/en/latest/specifications/version-specifiers).

    The schema and migration scripts support inclusion of other SQL files using `SOURCE '...';`
    directive similar to MariaDB/MySQL, so you can split out common portions in other files.
    However, do not place such files in `ybox.schema.migrate` package since the ones there
    are all required to be migration scripts that are executed in order, but you can use a
    sub-package inside it or elsewhere then use path relative to the schema/migration script.
    """

    # last version when versioning and schema migration did not exist
    _PRE_SCHEMA_VERSION = parse_version("0.9.0")
    # pattern to match "source '<file>'" in SQL script -- doesn't allow a quote in file name
    _SOURCE_SQLCMD_RE = re.compile(r"^\s*source\s*'([^']+)'\s*;\s*", re.IGNORECASE)

    # when comparing two container configurations, delete the sections mentioned below and the
    # keys in the [base] section (specifically log-file in log-opts will change)
    _CONFIG_NORMALIZE_DEL_SECTIONS = ["mounts", "configs", "env", "apps", "app_flags", "startup"]
    _CONFIG_NORMALIZE_DEL_BASE_KEYS = ["name", "includes", "home", "config_hardlinks",
                                       "log_driver", "log_opts"]

    def __init__(self, env: Environ):
        """
        Initialize connection to database and create tables+indexes if not present.

        :param env: the current Environ
        """
        # explicitly control transaction begin (in exclusive mode) since SERIALIZABLE isolation
        # level is required while sqlite3 module will not start transactions before reads
        self._conn = sqlite3.connect(f"{env.data_dir}/state.db", timeout=60,
                                     isolation_level=None)
        # create the initial tables
        with closing(cursor := self._conn.cursor()):
            self._begin_transaction(cursor)
            self._conn.create_function("REGEXP", 2, self.regexp, deterministic=True)
            self._conn.create_function("JSON_FROM_CSV", 1, self.json_from_csv, deterministic=True)
            self._conn.create_function("EQUIV_CONFIG", 2, self.equivalent_configuration,
                                       deterministic=True)
            self._init_schema(cursor)
            self._conn.commit()

    @staticmethod
    def regexp(pattern: str, val: str) -> int:
        """callable for the user-defined SQL REGEXP function"""
        # rely on python regex caching for efficient repeated calls
        return 1 if re.fullmatch(pattern, val) else 0

    @staticmethod
    def json_from_csv(val: str) -> str:
        """callable for the user-defined SQL JSON_FROM_CSV function"""
        return json.dumps(val.split(","))

    @staticmethod
    def equivalent_configuration(conf_str1: str, conf_str2: str) -> int:
        """
        Callable for the user-defined EQUIV_CONFIG function. Checking equivalence consists
        of deleting sections and keys that don't affect behavior of the container in terms
        of running the apps. Specifically the `log_opts` key from the `base` section has to
        be removed because the log-file name, when set based on time, will change in every run.
        """
        config1 = ConfigParser(allow_no_value=True, interpolation=None, delimiters="=")
        config1.optionxform = str  # type: ignore
        with StringIO(conf_str1) as conf_io1:
            config1.read_file(conf_io1)
        YboxStateManagement.normalize_configuration(config1)

        config2 = ConfigParser(allow_no_value=True, interpolation=None, delimiters="=")
        config2.optionxform = str  # type: ignore
        with StringIO(conf_str2) as conf_io2:
            config2.read_file(conf_io2)
        YboxStateManagement.normalize_configuration(config2)

        return int(config1 == config2)

    @staticmethod
    def normalize_configuration(config: ConfigParser) -> None:
        """
        Normalize a configuration by deleting sections/keys that do not affect its overall
        behavior in running applications in the container.
        """
        for del_section in YboxStateManagement._CONFIG_NORMALIZE_DEL_SECTIONS:
            config.remove_section(del_section)
        for del_key in YboxStateManagement._CONFIG_NORMALIZE_DEL_BASE_KEYS:
            config.remove_option("base", del_key)

    @staticmethod
    def _begin_transaction(cursor: sqlite3.Cursor) -> None:
        """
        Begin an EXCLUSIVE transaction to ensure atomicity of a group of reads and writes

        :param cursor: the `Cursor` object to use for execution
        """
        cursor.execute("BEGIN EXCLUSIVE TRANSACTION")

    def _init_schema(self, cursor: sqlite3.Cursor) -> None:
        """
        Initialize the required database objects or migrate from previous version.

        :param cursor: the `Cursor` object to use for execution
        """
        schema_pkg = files("ybox").joinpath("schema")
        ver_sep = ":"
        new_version = parse_version(ybox.__version__)
        # full initialization if empty database, else migrate and update the version if required
        # ('containers' table exists in all versions)
        if self._table_exists("containers", cursor):
            # check version and run migration scripts if required
            if self._table_exists("schema", cursor):  # version > 0.9.0
                cursor.execute("SELECT version FROM schema")
                old_version = parse_version(cursor.fetchone()[0])
            else:  # version = 0.9.0
                old_version = self._PRE_SCHEMA_VERSION
            if new_version != old_version:
                # determine the migration scripts that need to be run
                def check_version(file: str) -> bool:
                    """check if the versions in the given migration script (<ver1>:<ver2>.sql)
                       are within the stored schema version and current schema version"""
                    return all((version := parse_version(ver)) and old_version <= version <=
                               new_version for ver in file.removesuffix(".sql").split(ver_sep))

                scripts = [file for file in schema_pkg.joinpath("migrate").iterdir()
                           if file.is_file() and check_version(file.name)]
                if scripts:
                    if len(scripts) > 1:
                        scripts.sort(key=lambda f: parse_version(f.name[:f.name.find(ver_sep)]))
                    for script in scripts:
                        self._execute_script(script, cursor)
                # finally update the version
                cursor.execute("UPDATE schema SET version = ?", (str(new_version),))
        else:
            self._execute_script(schema_pkg.joinpath("init.sql"), cursor)
            cursor.execute("INSERT INTO schema VALUES (?)", (str(new_version),))

    @staticmethod
    def _table_exists(name: str, cursor: sqlite3.Cursor) -> bool:
        """
        Check if a given table exists.

        :param name: name of the table
        :param cursor: the `Cursor` object to use for execution
        :return: True if the table exists and False otherwise
        """
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                       "AND name = ?", (name,))
        return cursor.fetchone() is not None

    @staticmethod
    def _execute_script(sql_file: PathName, cursor: sqlite3.Cursor) -> None:
        """
        Execute a SQL script having one or more SQL commands. It also supports `source <file>`
        command (like MariaDB/MySQL) to include other SQL script files which can be a path
        relative to the original SQL script or an absolute path.

        :param sql_file: the SQL script file either as a `Path` or resource file from
                         importlib (`Traversable`)
        :param cursor: the `Cursor` object to use for execution
        """

        # process all the "source" directives and include file contents recursively
        def process_source(file: PathName, output_lines: list[str]) -> None:
            with file.open("r", encoding="utf-8") as sql_fd:
                while sql := sql_fd.readline():
                    if match := YboxStateManagement._SOURCE_SQLCMD_RE.fullmatch(sql):
                        inc = match.group(1)
                        inc_file = Path(inc) if os.path.isabs(inc) \
                            else file.parent.joinpath(inc)  # type: ignore
                        process_source(inc_file, output_lines)
                    else:
                        output_lines.append(sql)
                        # readline() will convert all line endings to \n except possibly the last
                        # one, so add a newline which might be missing in the middle of file
                        # in recursive "source" include
                        if sql[-1] != "\n":
                            output_lines.append("\n")

        sql_lines: list[str] = []
        process_source(sql_file, sql_lines)
        cursor.executescript("".join(sql_lines))

    def register_container(self, container_name: str, distribution: str, shared_root: str,
                           parser: ConfigParser,
                           force_own_orphans: bool) -> dict[str, Tuple[CopyType, dict[str, str]]]:
        """
        Register information of a ybox container including its name, distribution and
        configuration. In addition to the registration, this will also check for orphaned packages
        on the same `shared_root` (if applicable), and reassign them to this container if
        they were originally installed on a container having an :func:`equivalent_configuration`.

        :param container_name: name of the container
        :param distribution: the Linux distribution used when creating the container
        :param shared_root: the local shared root directory if `shared_root` flag is enabled
                            for the container
        :param parser: parser object for the configuration file used for creating the container
        :param force_own_orphans: if true, then force the ownership of orphan packages on the
                                  same shared root to this container even if the container
                                  configuration is not equivalent to the original container
                                  configuration under which those packages were installed
        :return: dictionary of previously installed packages (as the key) that got reassigned to
                 this container mapped to the `CopyType` for the wrapper files of those packages
                 (which can be used to recreate wrappers for container desktop/executable files)
        """
        packages: dict[str, Tuple[CopyType, dict[str, str]]] = {}
        # build the ini string from parser
        with StringIO() as config:
            parser.write(config)
            config.flush()
            config_str = config.getvalue()
        with closing(cursor := self._conn.cursor()):
            self._begin_transaction(cursor)
            # the ybox container may have been destroyed from outside ybox tools, so unregister
            self._unregister_container(container_name, cursor)
            cursor.execute("INSERT INTO containers VALUES (?, ?, ?, ?, false)",
                           (container_name, distribution, shared_root, config_str))
            # Find the orphan packages with the same shared_root and assign to this container
            # but only if the destroyed container had the same shared root and configuration.
            if shared_root:
                cursor.execute("SELECT dc.name FROM containers dc WHERE dc.destroyed = true AND "
                               f"dc.shared_root = ? AND ({force_own_orphans} OR "
                               "EQUIV_CONFIG(dc.configuration, ?))", (shared_root, config_str))
                equiv_destroyed = [row[0] for row in cursor.fetchall()]
                if equiv_destroyed:
                    in_args = ", ".join(["?" for _ in equiv_destroyed])
                    pkg_args = [container_name]
                    pkg_args.extend(equiv_destroyed)
                    # reassign packages to this container having matching destroyed container
                    cursor.execute("UPDATE packages SET container = ? WHERE container IN "
                                   f"({in_args}) RETURNING name, local_copy_type, flags", pkg_args)
                    packages = {name: (CopyType(cp_type), json.loads(flags)) for
                                (name, cp_type, flags) in cursor.fetchall()}
                    cursor.execute(
                        f"UPDATE package_deps SET container = ? WHERE container IN ({in_args})",
                        pkg_args)
                    # get rid of destroyed containers whose packages all got reassigned
                    cursor.execute(f"DELETE FROM containers WHERE name IN ({in_args})",
                                   equiv_destroyed)
            self._conn.commit()
            return packages

    def unregister_container(self, container_name: str) -> bool:
        """
        Unregister information of a ybox container. This also clears any registered packages
        for the container if 'shared_root' is false for the container. However, if 'shared_root'
        is true for the container, its packages are marked as "orphan" (i.e. owner was destroyed)
        if no other container refers to them. This is because the packages will still be visible
        in all other containers having 'shared_root' as enabled.

        :param container_name: name of the container
        :return: true if container was found in the database and removed
        """
        with closing(cursor := self._conn.cursor()):
            self._begin_transaction(cursor)
            result = self._unregister_container(container_name, cursor)
            self._conn.commit()
            return result

    @staticmethod
    def _unregister_container(container_name: str, cursor: sqlite3.Cursor) -> bool:
        """
        The real workhorse of `unregister_container`.

        :param container_name: name of the container
        :param cursor: the `Cursor` object to use for execution
        :return: true if container was found in the database and removed
        """
        cursor.execute("DELETE FROM containers WHERE name = ? RETURNING distribution, "
                       "shared_root, configuration", (container_name,))
        # if the container has 'shared_root', then packages will continue to exist, but update
        # the entry in `containers` with a new unique name (else there can be clashes later)
        # and update the container name in package tables
        row = cursor.fetchone()
        # check if there are any packages registered for the container
        cursor.execute("SELECT 1 FROM packages WHERE container = ?", (container_name,))
        if not cursor.fetchone():
            return row is not None

        distro, shared_root, config = row if row else (None, None, None)
        if shared_root:
            new_name = str(uuid4())  # generate a unique name
            insert_done = False
            while not insert_done:
                try:
                    cursor.execute("INSERT INTO containers VALUES (?, ?, ?, ?, true)",
                                   (new_name, distro, shared_root, config))
                    insert_done = True
                except sqlite3.IntegrityError:
                    # retry if unlucky (or buggy) to generate a UUID already generated in the past
                    new_name = str(uuid4())
            # UPDATE ... RETURNING gives the old value, hence getting local_copies separately
            # and then update both container name and empty local_copies in a single update
            cursor.execute("SELECT local_copies FROM packages WHERE container = ?",
                           (container_name,))
            local_copies = YboxStateManagement._extract_local_copies(cursor.fetchall())
            # update container name to the new one for destroyed container and clear local_copies
            cursor.execute("UPDATE packages SET container = ?, local_copies = '[]' "
                           "WHERE container = ?", (new_name, container_name))
            cursor.execute("UPDATE package_deps SET container = ? WHERE container = ?",
                           (new_name, container_name))
        else:
            cursor.execute("DELETE FROM packages WHERE container = ? RETURNING local_copies",
                           (container_name,))
            local_copies = YboxStateManagement._extract_local_copies(cursor.fetchall())
            cursor.execute("DELETE FROM package_deps WHERE container = ?", (container_name,))
        # remove the local wrapper files in both cases
        YboxStateManagement._remove_local_copies(local_copies)
        return row is not None

    def get_container_configuration(self, name: str) -> Optional[RuntimeConfiguration]:
        """
        Get the configuration details of the container which includes its Linux distribution name,
        shared root path (or empty if not using shared root), and its resolved configuration in
        INI format as a string.

        :param name: name of the container
        :return: configuration of the container as a `RuntimeConfiguration` object
        """
        with closing(cursor := self._conn.execute(
                "SELECT distribution, shared_root, configuration FROM containers WHERE name = ?",
                (name,))):
            row = cursor.fetchone()
            return RuntimeConfiguration(name=name, distribution=row[0], shared_root=row[1],
                                        ini_config=row[2]) if row else None

    def get_containers(self, name: Optional[str] = None, distribution: Optional[str] = None,
                       shared_root: Optional[str] = None) -> list[str]:
        """
        Get the containers matching the given name, distribution and/or shared root location.

        :param name: name of the container (optional)
        :param distribution: the Linux distribution used when creating the container (optional)
        :param shared_root: the local shared root directory to search for a package (optional)
        :return: list of containers matching the given criteria
        """
        predicate = ""
        args: list[str] = []
        if name:
            predicate = "name = ? AND "
            args.append(name)
        if distribution:
            predicate += "distribution = ? AND "
            args.append(distribution)
        if shared_root:
            predicate += "shared_root = ?"
            args.append(shared_root)
        else:
            predicate += "1=1"
        with closing(cursor := self._conn.execute(
                f"SELECT name FROM containers WHERE {predicate} ORDER BY name ASC", args)):
            rows = cursor.fetchall()
            return [str(row[0]) for row in rows]

    def register_package(self, container_name: str, package: str, local_copies: list[str],
                         copy_type: CopyType, app_flags: dict[str, str], shared_root: str,
                         dep_type: Optional[DependencyType], dep_of: str) -> None:
        """
        Register a package as being owned by a container.

        :param container_name: name of the container
        :param package: the package to be registered
        :param local_copies: list of locally wrapped files for the package (typically desktop
                             files and binary executables that invoke container ones)
        :param copy_type: the type of files (one of `CopyType`s or CopyType(0)) in `local_copies`
        :param app_flags: the flags from [app_flags] section and --app-flags option to add to
                          executable invocation in the local wrappers (`local_copies`)
        :param shared_root: the local shared root directory if `shared_root` flag is enabled
                            for the container
        :param dep_type: the `DependencyType` for the package, or None if not a dependency
        :param dep_of: if `dep_type` is not None, then this is the package that has this one
                       as a dependency of that type
        """
        with closing(cursor := self._conn.cursor()):
            self._begin_transaction(cursor)
            # if there is an entry for an orphaned package in the same shared root, then remove it
            if shared_root:
                # EXISTS query seems to be always faster than IN query in sqlite
                cursor.execute("""
                    DELETE from packages WHERE name = ? AND EXISTS (
                        SELECT 1 FROM containers dc WHERE dc.destroyed = true AND
                        dc.shared_root = ? AND packages.container = dc.name
                    ) RETURNING container""", (package, shared_root))
                if rows := cursor.fetchall():
                    in_str = ", ".join(["?" for _ in rows])
                    args = [package]
                    for row in rows:
                        args.append(row[0])
                    cursor.execute("DELETE from package_deps WHERE name = ? AND "
                                   f"container IN ({in_str})", args)
                    self._clean_destroyed_containers(cursor)
            cursor.execute("INSERT OR REPLACE INTO packages VALUES (?, ?, ?, ?, ?)",
                           (package, container_name, json.dumps(local_copies), copy_type.value,
                            json.dumps(app_flags)))
            if dep_type:
                self._register_dependency(container_name, dep_of, package, dep_type, cursor)
            self._conn.commit()

    def register_dependency(self, container_name: str, package: str, dependency: str,
                            dep_type: DependencyType) -> None:
        """
        Register a package dependency.

        :param container_name: name of the container
        :param package: the package whose dependency has to be registered
        :param dependency: the dependency to be registered
        :param dep_type: the `DependencyType` of the `dependency`
        """
        with closing(cursor := self._conn.cursor()):
            self._begin_transaction(cursor)
            self._register_dependency(container_name, package, dependency, dep_type, cursor)
            self._conn.commit()

    @staticmethod
    def _register_dependency(container_name: str, package: str, dependency: str,
                             dep_type: DependencyType, cursor: sqlite3.Cursor) -> None:
        """
        Internal method to register a package dependency.

        :param container_name: name of the container
        :param package: the package whose dependency has to be registered
        :param dependency: the dependency to be registered
        :param dep_type: the `DependencyType` of the `dependency`
        :param cursor: the `Cursor` object to use for execution
        """
        cursor.execute("INSERT OR REPLACE INTO package_deps VALUES (?, ?, ?, ?)",
                       (package, container_name, dependency, dep_type.value))

    def unregister_package(self, container_name: str, package: str,
                           shared_root: str) -> dict[str, DependencyType]:
        """
        Unregister a package for a given container and return its orphaned dependencies.

        :param container_name: name of the container
        :param package: the package to be unregistered
        :param shared_root: the local shared root directory if `shared_root` flag is enabled
                            for the container
        :return: dictionary of orphaned dependencies having name mapped to `DependencyType`
        """
        with closing(cursor := self._conn.cursor()):
            self._begin_transaction(cursor)
            # Query below determines dependent packages that have been orphaned as follows:
            # 1. select the dependencies of the package in the container/shared root
            # 2. select dependencies of all other packages having either the same shared root
            #    OR same container (latter if container is not on a shared root)
            # Select dependencies from 1 that do not exist in 2, i.e. no one else refers to them.
            # An equivalent query can be created using left outer join with null check, but it was
            # tested to be slower. An alternative query can be formed using window function
            # by partitioning on `dependency` column and applying an aggregate like count to
            # filter out those having only this package as the dependent. However, this
            # alternative is much slower probably due to sorting the entire table.
            # For reference, the no shared root query using window function looks like this:
            #   SELECT dependency, dep_type FROM (SELECT dependency, dep_type, COUNT()
            #     FILTER (WHERE name <> ?) OVER (PARTITION BY dependency)
            #     AS dep_counts FROM package_deps WHERE container = ?) WHERE dep_counts = 0
            o_deps_container_query = """
                SELECT 1 FROM package_deps d WHERE d.name <> ? AND d.container = ?"""
            o_deps_shared_root_query = """
                SELECT 1 FROM package_deps d INNER JOIN containers c
                ON (d.container = c.name AND d.name <> ?) WHERE c.shared_root = ?"""
            # EXISTS subquery for the containers with the same shared root as this container
            # which includes any destroyed container entries
            sr_exists = ("SELECT 1 FROM containers c WHERE c.shared_root = ? AND "
                         "p.container = c.name")
            pkgs_container_query = "container = ?"
            pkgs_shared_root_query = f"EXISTS ({sr_exists})"
            orphans_query = """
                SELECT dependency, dep_type FROM package_deps p WHERE name = ? AND {pkgs_loc_query}
                AND NOT EXISTS ({o_deps_query} AND p.dependency = d.dependency)"""
            if shared_root:
                cursor.execute(orphans_query.format(pkgs_loc_query=pkgs_shared_root_query,
                                                    o_deps_query=o_deps_shared_root_query),
                               (package, shared_root, package, shared_root))
            else:
                cursor.execute(orphans_query.format(pkgs_loc_query=pkgs_container_query,
                                                    o_deps_query=o_deps_container_query),
                               (package, container_name, package, container_name))
            orphans = {dep: DependencyType(dep_type) for (dep, dep_type) in cursor.fetchall()}

            # for the case of common shared root, delete package regardless of the container
            if shared_root:
                # delete from the packages table
                cursor.execute("DELETE FROM packages AS p WHERE name = ? AND EXISTS "
                               f"({sr_exists}) RETURNING local_copies", (package, shared_root))
                local_copies = self._extract_local_copies(cursor.fetchall())
                # and from the package_deps table (including dependency entries for the package)
                cursor.execute("DELETE FROM package_deps AS p WHERE "
                               f"(name = ? OR dependency = ?) AND EXISTS ({sr_exists})",
                               (package, package, shared_root))
                self._clean_destroyed_containers(cursor)
            else:
                # delete from the packages and package_deps tables
                cursor.execute("DELETE FROM packages WHERE name = ? AND container = ? "
                               "RETURNING local_copies", (package, container_name))
                local_copies = self._extract_local_copies(cursor.fetchall())
                cursor.execute("DELETE FROM package_deps WHERE (name = ? OR dependency = ?) "
                               "AND container = ?", (package, package, container_name))
            self._conn.commit()
        # delete all the files created locally for the container
        self._remove_local_copies(local_copies)
        return orphans

    @staticmethod
    def _clean_destroyed_containers(cursor: sqlite3.Cursor) -> None:
        """remove destroyed containers if there are no remaining packages for them"""
        cursor.execute("""
            DELETE FROM containers AS dc WHERE destroyed = true AND NOT EXISTS (
                SELECT 1 FROM packages p WHERE dc.name = p.container
            )""")

    @staticmethod
    def _extract_local_copies(rows: list, lc_idx: int = 0) -> list[str]:
        """
        Get a flattened list of local wrapper files from multiple rows having `local_copies`.

        :param rows: rows from `Cursor.fetchall()` or equivalent with `local_copies`
        :param lc_idx: index of the row having the `local_copies` field
        :return: flattened list of all the local wrapper files from the `local_copies` fields
        """
        # split local_copies field into an array using json then flatten
        return [file for row in rows if row[lc_idx] for file in json.loads(row[lc_idx]) if file]

    @staticmethod
    def _remove_local_copies(local_copies: list[str]) -> None:
        """remove the files created locally to run container executables"""
        for file in local_copies:
            print_warn(f"Removing local wrapper {file}")
            Path(file).unlink(missing_ok=True)

    def get_packages(self, container_name: str, regex: str = ".*",
                     dependency_type: str = ".*") -> list[str]:
        """
        Get the list of registered packages. This can be filtered for a specific container
        and using a (python) regular expression pattern as well as a regex for `DependencyType`.

        :param container_name: name of the container to filter packages
        :param regex: regular expression pattern to match against package names (optional)
        :param dependency_type: regular expression pattern to match against `dep_type` field if
                                the package is a dependency of another package (optional)
        :return: list of registered packages matching the given criteria
        """
        predicate = ""
        args: list[str] = []
        if container_name:
            predicate = "container = ? AND "
            args.append(container_name)
        if regex != ".*":
            predicate += "REGEXP(name, ?) AND "
            args.append(regex)
        if dependency_type == ".*":
            predicate += "1=1"
        elif not dependency_type:
            predicate += ("NOT EXISTS (SELECT 1 FROM package_deps WHERE "
                          "packages.container = container AND packages.name = dependency)")
        else:
            predicate += ("EXISTS (SELECT 1 FROM package_deps WHERE REGEXP(dep_type, ?) AND "
                          "packages.container = container AND packages.name = dependency)")
            args.append(dependency_type)
        with closing(cursor := self._conn.cursor()):
            cursor.execute(
                f"SELECT name FROM packages WHERE {predicate} ORDER BY name ASC", args)
            return [str(row[0]) for row in cursor.fetchall()]

    def check_packages(self, container_name: str, packages: typing.Iterable[str]) -> list[str]:
        """
        Check if given set of packages are in the state database, and return the list of
        the existing ones.

        :param container_name: name of the container to filter packages
        :param packages: list of packages to be checked
        :return: list of packages that are recorded in the state database
        """
        if not packages:
            return []
        in_list = ", ".join(["?" for _ in packages])
        args = [container_name]
        for pkg in packages:
            args.append(pkg)
        with closing(cursor := self._conn.cursor()):
            cursor.execute("SELECT name FROM packages pkgs "
                           f"WHERE pkgs.container = ? AND pkgs.name IN ({in_list})", args)
            return [str(row[0]) for row in cursor.fetchall()]

    def close(self) -> None:
        """Close the underlying connection to the database."""
        self._conn.rollback()  # rollback any pending transactions
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, ex_type, ex_value, ex_traceback):
        try:
            if ex_type:
                self._conn.rollback()
            else:
                self._conn.commit()
        finally:
            self._conn.close()
