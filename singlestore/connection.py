#!/usr/bin/env python
"""SingleStore database connections and cursors."""
from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Callable
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import Union
from urllib.parse import parse_qs
from urllib.parse import urlparse

import sqlparams

from . import drivers
from . import exceptions
from . import types
from .config import get_option
from .drivers.base import Driver
from .utils.convert_rows import convert_row
from .utils.convert_rows import convert_rows
from .utils.results import Description
from .utils.results import format_results
from .utils.results import Result


# DB-API settings
apilevel = '2.0'
threadsafety = 1
paramstyle = map_paramstyle = 'named'
positional_paramstyle = 'numeric'


def nested_converter(
    conv: Callable[[Any], Any],
    inner: Callable[[Any], Any],
) -> Callable[[Any], Any]:
    """Create a pipeline of two functions."""
    def converter(value: Any) -> Any:
        return conv(inner(value))
    return converter


def cast_bool_param(val: Any) -> bool:
    """Cast value to a bool."""
    if val is None or val is False:
        return False

    if val is True:
        return True

    # Test ints
    try:
        ival = int(val)
        if ival == 1:
            return True
        if ival == 0:
            return False
    except Exception:
        pass

    # Lowercase strings
    if hasattr(val, 'lower'):
        if val.lower() in ['on', 't', 'true', 'y', 'yes', 'enabled', 'enable']:
            return True
        elif val.lower() in ['off', 'f', 'false', 'n', 'no', 'disabled', 'disable']:
            return True

    raise ValueError('Unrecognized value for bool: {}'.format(val))


def build_params(**kwargs: Any) -> Dict[str, Any]:
    """
    Construct connection parameters from given URL and arbitrary parameters.

    Parameters
    ----------
    **kwargs : keyword-parameters, optional
        Arbitrary keyword parameters corresponding to connection parameters

    Returns
    -------
    dict

    """
    out: Dict[str, Any] = {}

    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    # Set known parameters
    for name in inspect.getfullargspec(connect).args:
        out[name] = kwargs.get(name, get_option(name))

    # See if host actually contains a URL; definitely not a perfect test.
    host = out['host']
    if host and (':' in host or '/' in host or '@' in host or '?' in host):
        urlp = _parse_url(host)
        if 'driver' not in urlp:
            urlp['driver'] = get_option('driver')
        out.update(urlp)

    out = _cast_params(out)

    # Set default port based on driver.
    if 'port' not in out or not out['port']:
        if out['driver'] == 'http':
            out['port'] = int(get_option('http_port') or 80)
        elif out['driver'] == 'https':
            out['port'] = int(get_option('http_port') or 443)
        else:
            out['port'] = int(get_option('port') or 3306)

    # If there is no user and the password is empty, remove the password key.
    if 'user' not in out and not out.get('password', None):
        out.pop('password', None)

    return out


def _get_param_types(func: Any) -> Dict[str, Any]:
    """
    Retrieve the types for the parameters to the given function.

    Note that if a parameter has multiple possible types, only the
    first one is returned.

    Parameters
    ----------
    func : callable
        Callable object to inspect the parameters of

    Returns
    -------
    dict

    """
    out = {}
    args = inspect.getfullargspec(func)
    for name in args.args:
        ann = args.annotations[name]
        if isinstance(ann, str):
            ann = eval(ann)
        if hasattr(ann, '__args__'):
            out[name] = ann.__args__[0]
        else:
            out[name] = ann
    return out


def _cast_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Cast known keys to appropriate values.

    Parameters
    ----------
    params : dict
        Dictionary of connection parameters

    Returns
    -------
    dict

    """
    param_types = _get_param_types(connect)
    out = {}
    for key, val in params.items():
        key = key.lower()
        if val is None:
            continue
        if key not in param_types:
            raise ValueError('Unrecognized connection parameter: {}'.format(key))
        dtype = param_types[key]
        if dtype is bool:
            val = cast_bool_param(val)
        else:
            val = dtype(val)
        out[key] = val
    return out


def _parse_url(url: str) -> Dict[str, Any]:
    """
    Parse a connection URL and return only the defined parts.

    Parameters
    ----------
    url : str
        The URL passed in can be a full URL or a partial URL. At a minimum,
        a host name must be specified. All other parts are optional.

    Returns
    -------
    dict

    """
    out: Dict[str, Any] = {}

    if '//' not in url:
        url = '//' + url

    parts = urlparse(url, scheme='singlestore', allow_fragments=True)

    url_db = parts.path
    if url_db.startswith('/'):
        url_db = url_db.split('/')[1].strip()
    url_db = url_db.split('/')[0].strip() or ''

    # Retrieve basic connection parameters
    out['host'] = parts.hostname or None
    out['port'] = parts.port or None
    out['database'] = url_db or None
    out['user'] = parts.username or None

    # Allow an empty string for password
    if out['user'] and parts.password is not None:
        out['password'] = parts.password

    if parts.scheme != 'singlestore':
        out['driver'] = parts.scheme.lower()

    # Convert query string to parameters
    out.update({k.lower(): v[-1] for k, v in parse_qs(parts.query).items()})

    return {k: v for k, v in out.items() if v is not None}


def _name_check(name: str) -> str:
    """
    Make sure the given name is a legal variable name.

    Parameters
    ----------
    name : str
        Name to check

    Returns
    -------
    str

    """
    name = name.strip()
    if not re.match(r'^[A-Za-z_][\w+_]*$', name):
        raise ValueError('Name contains invalid characters')
    return name


class Cursor(object):
    """
    Database cursor for submitting commands and queries.

    This object should not be instantiated directly.
    The `Connection.cursor` method should be used.

    Parameters
    ----------
    connection : Connection
        The connection the cursor belongs to
    cursor : Cursor
        The Cursor object from the underlying MySQL package
    driver : Driver
        Driver of the current database connector

    Returns
    -------
    Cursor

    """

    arraysize: int = 100

    def __init__(
            self, connection: Connection, cursor: Any, driver: Driver,
    ):
        self.errorhandler = connection.errorhandler
        self._conn: Optional[Connection] = connection
        self._cursor = cursor
        self._driver = driver
        self.rownumber: Optional[int] = None
        self.description: Optional[List[Description]] = None
        self.arraysize = type(self).arraysize
        self._many_queries: Optional[Iterator[Any]] = None
        self._format: str = get_option('results.format')
        self._convetrers: List[Any] = []

    @property
    def connection(self) -> Optional[Connection]:
        """
        Return the connection that the cursor belongs to.

        Returns
        -------
        Connection or None

        """
        return self._conn

    def _set_description(self) -> None:
        """
        Return column descriptions for the current result set.

        Returns
        -------
        list of Description

        """
        if self._cursor.description:
            self._converters = []
            out = []
            for i, item in enumerate(self._cursor.description):
                item = list(item) + [None, None]
                item[1] = types.ColumnType.get_code(item[1])
                item[6] = not(not(item[6]))
                out.append(Description(*item[:9]))

                # Setup override converters, if the SET flag is set use that
                # converter but keep the same type code.
                if item[7] and item[7] & 2048:  # SET_FLAG = 2048
                    conv = self._driver.converters.get(247, None)  # SET CODE = 247
                else:
                    conv = self._driver.converters.get(item[1], None)
                if conv is not None:
                    self._converters.append((i, conv))

            self.description = out

    @property
    def rowcount(self) -> int:
        """
        Return the number of rows the last execute produced or affected.

        Returns
        -------
        int

        """
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')
        return getattr(self._cursor, 'rowcount', getattr(self._cursor, '_rowcount', -1))

    def callproc(
        self, name: str,
        params: Optional[Sequence[Any]] = None,
    ) -> None:
        """
        Call a stored procedure.

        Parameters
        ----------
        name : str
            Name of the stored procedure
        params : iterable or dict, optional
            Parameters to the stored procedure

        """
        # NOTE: The `callproc` interface varies quite a bit between drivers
        #       so it is implemented using `execute` here.

        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        name = _name_check(name)

        if not params:
            self.execute(f'CALL {name}();')
        else:
            keys = ', '.join([f':{i+1}' for i in range(len(params))])
            self.execute(f'CALL {name}({keys});', params)

    def close(self) -> None:
        """Close the cursor."""
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        try:
            self._cursor.close()

        # Ignore weak reference errors. It just means the connection
        # was closed underneath us.
        except ReferenceError:
            pass

        except Exception as exc:
            raise self._driver.convert_exception(exc)

        self._cursor = None
        self._conn = None

    def execute(
        self, oper: str,
        params: Optional[Union[Sequence[Any], Mapping[str, Any]]] = None,
    ) -> None:
        """
        Execute a SQL statement.

        Parameters
        ----------
        oper : str
            The SQL statement to execute
        params : iterable or dict, optional
            Parameters to substitute into the SQL code

        """
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        self.description = None
        self.rownumber = None

        try:
            if params:
                param_converter = sqlparams.SQLParams(
                    isinstance(params, Mapping) and
                    map_paramstyle or positional_paramstyle,
                    self._driver.dbapi.paramstyle,
                    escape_char=True,
                )
                self._cursor.execute(*param_converter.format(oper, params))
            else:
                self._cursor.execute(oper)
        except Exception as exc:
            raise self._driver.convert_exception(exc)

        self._set_description()

        self.rownumber = 0

    def executemany(
        self, oper: str,
        param_seq: Optional[Sequence[Union[Sequence[Any], Mapping[str, Any]]]] = None,
    ) -> None:
        """
        Execute SQL code against multiple sets of parameters.

        Parameters
        ----------
        oper : str
            The SQL statement to execute
        params_seq : iterable of iterables or dicts, optional
            Sets of parameters to substitute into the SQL code

        """
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        self.description = None
        self.rownumber = None

        param_seq = param_seq or [[]]

        try:
            # NOTE: Just implement using `execute` to cover driver inconsistencies
            if param_seq[0]:
                self._many_queries = ((oper, params) for params in param_seq)
                self.execute(*next(self._many_queries))
            else:
                self.execute(oper)

        except Exception as exc:
            raise self._driver.convert_exception(exc)

        self._set_description()

        self.rownumber = 0

    def fetchone(self) -> Optional[Result]:
        """
        Fetch a single row from the result set.

        Returns
        -------
        tuple
            Values of the returned row if there are rows remaining

        """
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        try:
            out = self._cursor.fetchone()
        except Exception as exc:
            raise self._driver.convert_exception(exc)

        if out is not None and self.rownumber is not None:
            self.rownumber += 1

        if out is not None:
            out = convert_row(tuple(out), self._converters)

        return format_results(self._format, self.description or [], out, single=True)

    def fetchmany(self, size: Optional[int] = None) -> Result:
        """
        Fetch `size` rows from the result.

        If `size` is not specified, the `arraysize` attribute is used.

        Returns
        -------
        list of tuples
            Values of the returned rows if there are rows remaining

        """
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        if size is not None:
            size = max(int(size), 1)
        else:
            size = max(int(self.arraysize), 1)

        try:
            # This is to get around a bug in mysql.connector. For some reason,
            # fetchmany(1) returns the same row over and over again.
            if size == 1:
                out = [self._cursor.fetchone()]
            else:
                # Don't use a keyword parameter for size=. Pyodbc fails with that.
                out = self._cursor.fetchmany(size)
        except Exception as exc:
            raise self._driver.convert_exception(exc)

        out = convert_rows(out, self._converters)

        formatted: Result = format_results(
            self._format, self.description or [], out,
        )

        if self.rownumber is not None:
            self.rownumber += len(formatted)

        return formatted

    def fetchall(self) -> Result:
        """
        Fetch all rows in the result set.

        Returns
        -------
        list of tuples
            Values of the returned rows if there are rows remaining
        None
            If there are no rows to return

        """
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        try:
            out = self._cursor.fetchall()
        except Exception as exc:
            raise self._driver.convert_exception(exc)

        out = convert_rows(out, self._converters)

        formatted: Result = format_results(
            self._format, self.description or [], out,
        )

        if self.rownumber is not None:
            self.rownumber += len(formatted)

        return formatted

    def nextset(self) -> Optional[bool]:
        """Skip to the next available result set."""
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        self.rownumber = None

        # If we still have `executemany` results pending, use those
        if self._many_queries is not None:
            try:
                self.execute(*next(self._many_queries))
                return True
            except StopIteration:
                self._many_queries = None
                return False

        try:
            out = self._cursor.nextset()
            self._set_description()
            if out:
                self.rownumber = 0
                return True
            return False

        except Exception as exc:
            exc = self._driver.convert_exception(exc)
            if getattr(exc, 'errno', -1) == 2053:
                return False
            self.rownumber = 0
            return True

    def setinputsizes(self, sizes: Sequence[int]) -> None:
        """Predefine memory areas for parameters."""
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        try:
            self._cursor.setinputsizes(sizes)
        except Exception as exc:
            raise self._driver.convert_exception(exc)

    def setoutputsize(self, size: int, column: Optional[str] = None) -> None:
        """Set a column buffer size for fetches of large columns."""
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        try:
            self._cursor.setoutputsize(size, column)
        except Exception as exc:
            raise self._driver.convert_exception(exc)

    def scroll(self, value: int, mode: str = 'relative') -> None:
        """
        Scroll the cursor to the position in the result set.

        Parameters
        ----------
        value : int
            Value of the positional move
        mode : str
            Where to move the cursor from: 'relative' or 'absolute'

        """
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        value = int(value)
        try:
            self._cursor.scroll(value, mode=mode)
            if self.rownumber is not None:
                if mode == 'relative':
                    self.rownumber += value
                elif mode == 'absolute':
                    self.rownumber = value
                else:
                    raise ValueError(f'Unrecognized scroll mode {mode}')
        except Exception as exc:
            raise self._driver.convert_exception(exc)

    @property
    def messages(self) -> Sequence[tuple[int, str]]:
        """
        List of received messages.

        Returns
        -------
        list of tuples
            Tuples contain a numeric code and a message

        """
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        return self._cursor.messages

    def next(self) -> Optional[Result]:
        """
        Return the next row from the result set for use in iterators.

        Returns
        -------
        tuple of values
            If a row of results exists
        None
            If there are no more results

        """
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        try:
            out = self.fetchone()
            if out is None:
                raise StopIteration
            return out
        except StopIteration:
            raise
        except Exception as exc:
            raise self._driver.convert_exception(exc)

    __next__ = next

    def __iter__(self) -> Any:
        """Return result iterator."""
        return self

    @property
    def lastrowid(self) -> Optional[int]:
        """Return the rowid of the last modified row."""
        if self._cursor is None:
            raise exceptions.InterfaceError(2048, 'Cursor is closed.')

        return getattr(
            self._cursor, 'lastrowid',
            getattr(self._cursor, '_lastrowid', None),
        ) or None

    def __enter__(self) -> Cursor:
        """Enter a context."""
        return self

    def __exit__(
        self, exc_type: Optional[object],
        exc_value: Optional[Exception], exc_traceback: Optional[str],
    ) -> None:
        """Exit a context."""
        self.close()

    def is_connected(self) -> bool:
        """
        Check if the cursor is connected.

        Returns
        -------
        bool

        """
        if self._conn is None:
            return False
        return self._conn.is_connected()


class Connection(object):
    """
    SingleStore database connection.

    Instances of this object are typically created through the
    `connect` function rather than creating them directly.
    See the `connect` function for parameter definitions.


    Examples
    --------
    # Standard database connection
    >>> conn = s2.connect('me:p455w0rd@s2-host.com/my_db')

    # Connect to HTTP API on port 8080
    >>> conn = s2.connect('http://me:p455w0rd@s2-host.com:8080/my_db')

    See Also
    --------
    `connect`

    """

    Warning = exceptions.Warning
    Error = exceptions.Error
    InterfaceError = exceptions.InterfaceError
    DataError = exceptions.DataError
    DatabaseError = exceptions.DatabaseError
    OperationalError = exceptions.OperationalError
    IntegrityError = exceptions.IntegrityError
    InternalError = exceptions.InternalError
    ProgrammingError = exceptions.ProgrammingError
    NotSupportedError = exceptions.NotSupportedError

    def __init__(self, **kwargs: Any):
        kwargs = build_params(**kwargs)

        self._conn: Optional[Any] = None
        self.errorhandler = None
        self.connection_params: Dict[str, Any] = build_params(**kwargs)

        drv_name = re.sub(r'^\w+\+', r'', self.connection_params['driver']).lower()
        self._driver = drivers.get_driver(drv_name, self.connection_params)
        # TODO: converters parameter
        self._merge_converters(None)

        try:
            self._conn = self._driver.connect()
        except Exception as exc:
            raise self._driver.convert_exception(exc)

    def _merge_converters(
        self,
        user_converters: Optional[Dict[int, Callable[[Any], Any]]] = None,
    ) -> None:
        """Merge user-defined converters with driver converters."""
        self._converters = dict(self._driver.converters)
        for code, conv in (user_converters or {}).items():
            if conv is None:
                continue
            if code in self._converters:
                self._converters[code] = nested_converter(conv, self._converters[code])
            else:
                self._converters[code] = conv

    def autocommit(self, value: bool = True) -> None:
        """Set autocommit mode."""
        if self._conn is None:
            raise exceptions.InterfaceError(2048, 'Connection is closed.')
        self.set_session_var(autocommit=bool(value))

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is None:
            return None
        try:
            self._conn.close()
        except Exception as exc:
            raise self._driver.convert_exception(exc)
        finally:
            self._conn = None

    def commit(self) -> None:
        """Commit the pending transaction."""
        if self._conn is None:
            raise exceptions.InterfaceError(2048, 'Connection is closed.')
        try:
            self._conn.commit()
        except Exception as exc:
            raise self._driver.convert_exception(exc)

    def rollback(self) -> None:
        """Rollback the pending transaction."""
        if self._conn is None:
            raise exceptions.InterfaceError(2048, 'Connection is closed.')
        try:
            self._conn.rollback()
        except Exception as exc:
            raise self._driver.convert_exception(exc)

    def cursor(self) -> Cursor:
        """
        Create a new cursor object.

        Returns
        -------
        Cursor

        """
        if self._conn is None:
            raise exceptions.InterfaceError(2048, 'Connection is closed.')
        try:
            cur = self._conn.cursor()
        except Exception as exc:
            raise self._driver.convert_exception(exc)
        return Cursor(self, cur, self._driver)

    def _i_cursor(self) -> Cursor:
        """
        Create a cursor for internal use.

        Internal cursors always return tuples in results.
        These are used to ensure that methods that query the database
        have a consistent results structure regardless of the
        `results.format` option.

        Returns
        -------
        Cursor

        """
        out = self.cursor()
        out._format = 'tuple'
        return out

    @property
    def messages(self) -> Sequence[tuple[int, str]]:
        """
        Return messages generated by the connection.

        Returns
        -------
        list of tuples
            Each tuple contains an int code and a message

        """
        if self._conn is None:
            raise exceptions.InterfaceError(2048, 'Connection is closed.')
        return self._conn.messages

    def __enter__(self) -> Connection:
        """Enter a context."""
        return self

    def __exit__(
        self, exc_type: Optional[object],
        exc_value: Optional[Exception], exc_traceback: Optional[str],
    ) -> None:
        """Exit a context."""
        self.close()

    def is_connected(self) -> bool:
        """
        Determine if the database is still connected.

        Returns
        -------
        bool

        """
        if self._conn is None:
            return False
        try:
            return self._driver.is_connected(self._conn)
        except Exception as exc:
            raise self._driver.convert_exception(exc)

    def set_global_var(self, **kwargs: Any) -> None:
        """
        Set one or more global variables in the database.

        Parameters
        ----------
        **kwargs : key-value pairs
            Keyword parameters specify the variable names and values to set

        """
        if self._conn is None:
            raise exceptions.InterfaceError(2048, 'Connection is closed.')
        with self._i_cursor() as cur:
            for name, value in kwargs.items():
                if value is True:
                    value = 'ON'
                elif value is False:
                    value = 'OFF'
                name = _name_check(name)
                cur.execute('set global {}=:1;'.format(name), [value])

    def set_session_var(self, **kwargs: Any) -> None:
        """
        Set one or more session variables in the database.

        Parameters
        ----------
        **kwargs : key-value pairs
            Keyword parameters specify the variable names and values to set

        """
        if self._conn is None:
            raise exceptions.InterfaceError(2048, 'Connection is closed.')
        with self._i_cursor() as cur:
            for name, value in kwargs.items():
                if value is True:
                    value = 'ON'
                elif value is False:
                    value = 'OFF'
                name = _name_check(name)
                cur.execute('set session {}=:1;'.format(name), [value])

    def get_global_var(self, name: str) -> Any:
        """
        Retrieve the value of a global variable.

        Returns
        -------
        Any

        """
        if self._conn is None:
            raise exceptions.InterfaceError(2048, 'Connection is closed.')
        name = _name_check(name)
        with self._i_cursor() as cur:
            cur.execute('show global variables like "{}";'.format(name))
            out = list(cur)[0][1]
            if out.lower() in ['on', 'true']:
                return True
            elif out.lower() in ['off', 'false']:
                return False
            return out

    def get_session_var(self, name: str) -> Any:
        """
        Retrieve the value of a session variable.

        Returns
        -------
        Any

        """
        if self._conn is None:
            raise exceptions.InterfaceError(2048, 'Connection is closed.')
        name = _name_check(name)
        with self._i_cursor() as cur:
            cur.execute('show local variables like "{}";'.format(name))
            out = list(cur)[0][1]
            if out.lower() in ['on', 'true']:
                return True
            elif out.lower() in ['off', 'false']:
                return False
            return out

    def enable_http_api(self, port: Optional[int] = None) -> int:
        """
        Enable the HTTP API in the server.

        Use of this method requires privileges that allow setting global
        variables and starting the HTTP proxy.

        Parameters
        ----------
        port : int, optional
            The port number that the HTTP server should run on. If this
            value is not specified, the current value of the
            `http_proxy_port` is used.

        Returns
        -------
        int
            port number of the HTTP server

        """
        if self._conn is None:
            raise exceptions.InterfaceError(2048, 'Connection is closed.')
        with self._i_cursor() as cur:
            if port is not None:
                self.set_global_var(http_proxy_port=int(port))
            self.set_global_var(http_api=True)
            cur.execute('restart proxy')
            return self.get_global_var('http_proxy_port')

    def disable_http_api(self) -> None:
        """Disable the HTTP API."""
        if self._conn is None:
            raise exceptions.InterfaceError(2048, 'Connection is closed.')
        with self._i_cursor() as cur:
            self.set_global_var(http_api=False)
            cur.execute('restart proxy')


#
# NOTE: When adding parameters to this function, you should always
#       make the value optional with a default of None. The options
#       processing framework will fill in the default value based
#       on environment variables or other configuration sources.
#
def connect(
    host: Optional[str] = None, user: Optional[str] = None,
    password: Optional[str] = None, port: Optional[int] = None,
    database: Optional[str] = None, driver: Optional[str] = None,
    pure_python: Optional[bool] = None, local_infile: Optional[bool] = None,
    odbc_driver: Optional[str] = None, charset: Optional[str] = None,
) -> Connection:
    """
    Return a SingleStore database connection.

    Parameters
    ----------
    host : str, optional
        Hostname, IP address, or URL that describes the connection.
        The scheme or protocol defines which database connector to use.
        By default, the `mysql-connector` scheme is used. To connect to the
        HTTP API, the scheme can be set to `http` or `https`. The username,
        password, host, and port are specified as in a standard URL. The path
        indicates the database name. The overall form of the URL is:
        `scheme://user:password@host:port/db_name`.  The scheme can
        typically be left off (unless you are using the HTTP API):
        `user:password@host:port/db_name`.
    user : str, optional
        Database user name
    password : str, optional
        Database user password
    port : int, optional
        Database port. This defaults to 3306 for non-HTTP connections, 80
        for HTTP connections, and 443 for HTTPS connections.
    database : str, optional
        Database name
    pure_python : bool, optional
        Use the connector in pure Python mode
    local_infile : bool, optional
        Allow local file uploads
    odbc_driver : str, optional
        Name of the ODBC driver to use for ODBC connections
    charset : str, optional
        Character set for string values

    Examples
    --------
    # Standard database connection
    >>> conn = s2.connect('me:p455w0rd@s2-host.com/my_db')

    # Connect to HTTP API on port 8080
    >>> conn = s2.connect('http://me:p455w0rd@s2-host.com:8080/my_db')

    Returns
    -------
    Connection

    """
    return Connection(**dict(locals()))
