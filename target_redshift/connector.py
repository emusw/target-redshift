"""Common SQL connectors for Streams and Sinks."""

from __future__ import annotations
import typing as t
from typing import cast

import simplejson
from contextlib import contextmanager

from singer_sdk.typing import _jsonschema_type_check
from singer_sdk import typing as th
from singer_sdk.connectors import SQLConnector
from singer_sdk.helpers._typing import get_datelike_property_type
from redshift_connector import Cursor
import redshift_connector

from sqlalchemy.engine.url import URL
from sqlalchemy_redshift.dialect import SUPER, BIGINT, VARCHAR
from sqlalchemy.types import (
    BOOLEAN,
    DATE,
    DATETIME,
    DECIMAL,
    INTEGER,
    TEXT,
    TIME,
    TIMESTAMP,
    VARCHAR,
    TypeDecorator,
)
from sqlalchemy.schema import CreateTable, DropTable, CreateSchema
from sqlalchemy.types import TypeEngine
from sqlalchemy import Table, MetaData, DDL, Column


class RedshiftConnector(SQLConnector):
    """Sets up SQL Alchemy, and other Postgres related stuff."""

    allow_column_add: bool = True  # Whether ADD COLUMN is supported.
    allow_column_rename: bool = True  # Whether RENAME COLUMN is supported.
    allow_column_alter: bool = False  # Whether altering column types is supported.
    allow_merge_upsert: bool = True  # Whether MERGE UPSERT is supported.
    allow_temp_tables: bool = True  # Whether temp tables are supported.

    def create_schema(self, schema_name: str) -> None:
        """Create target schema.

        Args:
            schema_name: The target schema to create.
        """
        with self._connect_cursor() as cursor:
            cursor.execute(str(CreateSchema(schema_name)))

    @contextmanager
    def _connect_cursor(self) -> t.Iterator[Cursor]:
        with redshift_connector.connect(
            user=self.config["user"],
            password=self.config["password"],
            host=self.config["host"],
            port=self.config["port"],
            database=self.config["dbname"],
        ) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                yield cursor

    def prepare_table(  # type: ignore[override]
        self,
        full_table_name: str,
        schema: dict,
        primary_keys: t.Sequence[str],
        cursor: Cursor,
        partition_keys: list[str] | None = None,
        as_temp_table: bool = False,
    ) -> Table:
        """Adapt target table to provided schema if possible.

        Args:
            full_table_name: the target table name.
            schema: the JSON Schema for the table.
            primary_keys: list of key properties.
            connection: the database connection.
            partition_keys: list of partition keys.
            as_temp_table: True to create a temp table.

        Returns:
            The table object.
        """
        _, schema_name, table_name = self.parse_full_table_name(full_table_name)
        meta = MetaData(schema=schema_name)
        table: Table
        if not self.table_exists(full_table_name=full_table_name):
            table = self.create_empty_table(
                table_name=table_name,
                meta=meta,
                schema=schema,
                primary_keys=primary_keys,
                partition_keys=partition_keys,
                as_temp_table=as_temp_table,
                cursor=cursor,
            )
            return table

        with self._connect() as connection, connection.begin():
            meta.reflect(connection, only=[table_name])
        table = meta.tables[full_table_name]  # So we don't mess up the casing of the Table reference

        columns = self.get_table_columns(full_table_name=full_table_name)

        for property_name, property_def in schema["properties"].items():
            column_object = None
            if property_name in columns:
                column_object = columns[property_name]
            self.prepare_column(
                full_table_name=table.fullname,
                column_name=property_name,
                sql_type=self.to_sql_type(property_def),
                cursor=cursor,
                column_object=column_object,
            )

        return meta.tables[full_table_name]

    def copy_table_structure(
        self,
        full_table_name: str,
        from_table: Table,
        cursor: Cursor,
        as_temp_table: bool = False,
    ) -> Table:
        """Copy table structure.

        Args:
            full_table_name: the target table name potentially including schema
            from_table: the  source table
            connection: the database connection.
            as_temp_table: True to create a temp table.

        Returns:
            The new table object.
        """
        _, schema_name, table_name = self.parse_full_table_name(full_table_name)
        # schema_name = "meltano_test"
        meta = MetaData(schema=schema_name)
        new_table: Table
        columns = []
        if self.table_exists(full_table_name=full_table_name):
            raise RuntimeError("Table already exists")
        for column in from_table.columns:
            columns.append(column._copy())
        if as_temp_table:
            new_table = Table(table_name, meta, *columns, prefixes=["TEMPORARY"])
        else:
            new_table = Table(table_name, meta, *columns)

        create_table_ddl = str(CreateTable(new_table).compile(dialect=self._engine.dialect))
        cursor.execute(create_table_ddl)
        return new_table

    def drop_table(self, table: Table, cursor: Cursor):
        """Drop table data."""
        drop_table_ddl = str(DropTable(table).compile(dialect=self._engine.dialect))
        cursor.execute(drop_table_ddl)

    def to_sql_type(self, jsonschema_type: dict) -> TypeEngine:
        """Convert JSON Schema type to a SQL type.

        Args:
            jsonschema_type: The JSON Schema object.

        Returns:
            The SQL type.
        """
        if _jsonschema_type_check(jsonschema_type, ("string",)):
            datelike_type = get_datelike_property_type(jsonschema_type)
            if datelike_type:
                if datelike_type == "date-time":
                    return DATETIME()
                if datelike_type in "time":
                    return TIME()
                if datelike_type == "date":
                    return DATE()

            maxlength = jsonschema_type.get("maxLength")
            return VARCHAR(maxlength)

        if _jsonschema_type_check(jsonschema_type, ("integer",)):
            return BIGINT()
        if _jsonschema_type_check(jsonschema_type, ("number",)):
            return DECIMAL()
        if _jsonschema_type_check(jsonschema_type, ("boolean",)):
            return BOOLEAN()

        if _jsonschema_type_check(jsonschema_type, ("object", "array")):
            return SUPER()

        return VARCHAR()

    def create_empty_table(  # type: ignore[override]
        self,
        table_name: str,
        meta: MetaData,
        schema: dict,
        cursor: Cursor,
        primary_keys: t.Sequence[str] | None = None,
        partition_keys: list[str] | None = None,
        as_temp_table: bool = False,
    ) -> Table:
        """Create an empty target table.

        Args:
            table_name: the target table name.
            meta: the SQLAchemy metadata object.
            schema: the JSON schema for the new table.
            cursor: the database cursor.
            primary_keys: list of key properties.
            partition_keys: list of partition keys.
            as_temp_table: True to create a temp table.

        Returns:
            The new table object.

        Raises:
            NotImplementedError: if temp tables are unsupported and as_temp_table=True.
            RuntimeError: if a variant schema is passed with no properties defined.
        """
        columns: list[Column] = []
        primary_keys = primary_keys or []
        try:
            properties: dict = schema["properties"]
        except KeyError:
            raise RuntimeError(f"Schema for table_name: '{table_name}'" f"does not define properties: {schema}")

        for property_name, property_jsonschema in properties.items():
            is_primary_key = property_name in primary_keys
            columns.append(
                Column(
                    property_name,
                    self.to_sql_type(property_jsonschema),
                    primary_key=is_primary_key,
                    autoincrement=False,  # See: https://github.com/MeltanoLabs/target-postgres/issues/193 # noqa: E501
                )
            )
        if as_temp_table:
            new_table = Table(table_name, meta, *columns, prefixes=["TEMPORARY"])
        else:
            new_table = Table(table_name, meta, *columns)

        create_table_ddl = str(CreateTable(new_table).compile(dialect=self._engine.dialect))
        cursor.execute(create_table_ddl)
        return new_table

    def prepare_column(
        self,
        full_table_name: str,
        column_name: str,
        sql_type: TypeEngine,
        cursor: Cursor,
        column_object: Column | None = None,
    ) -> None:
        """Adapt target table to provided schema if possible.

        Args:
            full_table_name: the fully qualified table name.
            column_name: the target column name.
            sql_type: the SQLAlchemy type.
            cursor: a database cursor.
            column_object: a SQLAlchemy column. optional.
        """
        _, schema_name, table_name = self.parse_full_table_name(full_table_name)

        column_name = column_name.lower().replace(" ", "_")
        column_exists = column_object is not None or self.column_exists(full_table_name, column_name)

        if not column_exists:
            self._create_empty_column(
                # We should migrate every function to use Table
                # instead of having to know what the function wants
                full_table_name=full_table_name,
                column_name=column_name,
                sql_type=sql_type,
                schema_name=cast(str, schema_name),
                cursor=cursor,
            )
            return

        self._adapt_column_type(
            full_table_name=full_table_name,
            column_name=column_name,
            sql_type=sql_type,
            cursor=cursor,
        )

    def _create_empty_column(
        self,
        full_table_name: str,
        column_name: str,
        sql_type: TypeEngine,
        cursor: Cursor,
    ) -> None:
        """Create a new column.

        Args:
            full_table_name: The target table name.
            column_name: The name of the new column.
            sql_type: SQLAlchemy type engine to be used in creating the new column.

        Raises:
            NotImplementedError: if adding columns is not supported.
        """
        if not self.allow_column_add:
            msg = "Adding columns is not supported."
            raise NotImplementedError(msg)

        _, schema_name, table_name = self.parse_full_table_name(full_table_name)
        column_add_ddl = self.get_column_add_ddl(
            table_name=table_name,
            schema_name=schema_name,
            column_name=column_name,
            column_type=sql_type,
        )
        cursor.execute(column_add_ddl)

    def get_column_add_ddl(  # type: ignore[override]
        self,
        table_name: str,
        schema_name: str,
        column_name: str,
        column_type: TypeEngine,
    ) -> DDL:
        """Get the create column DDL statement.

        Args:
            table_name: Fully qualified table name of column to alter.
            schema_name: Schema name.
            column_name: Column name to create.
            column_type: New column sqlalchemy type.

        Returns:
            A sqlalchemy DDL instance.
        """
        column = Column(column_name, column_type)

        return DDL(
            ('ALTER TABLE "%(schema_name)s"."%(table_name)s"' "ADD COLUMN %(column_name)s %(column_type)s"),
            {
                "schema_name": schema_name,
                "table_name": table_name,
                "column_name": column.compile(dialect=self._engine.dialect),
                "column_type": column.type.compile(dialect=self._engine.dialect),
            },
        )

    def _adapt_column_type(
        self,
        full_table_name: str,
        column_name: str,
        sql_type: TypeEngine,
        cursor: Cursor,
    ) -> None:
        """Adapt table column type to support the new JSON schema type.

        Args:
            full_table_name: The target table name.
            column_name: The target column name.
            sql_type: The new SQLAlchemy type.

        Raises:
            NotImplementedError: if altering columns is not supported.
        """
        current_type: TypeEngine = self._get_column_type(
            full_table_name,
            column_name,
        )

        # remove collation if present and save it
        current_type_collation = self.remove_collation(current_type)

        # Check if the existing column type and the sql type are the same
        if str(sql_type) == str(current_type):
            # The current column and sql type are the same
            # Nothing to do
            return

        # Not the same type, generic type or compatible types
        # calling merge_sql_types for assistnace
        compatible_sql_type = self.merge_sql_types([current_type, sql_type])

        if str(compatible_sql_type) == str(current_type):
            # Nothing to do
            return

        # Put the collation level back before altering the column
        if current_type_collation:
            self.update_collation(compatible_sql_type, current_type_collation)

        if not self.allow_column_alter:
            msg = (
                "Altering columns is not supported. Could not convert column "
                f"'{full_table_name}.{column_name}' from '{current_type}' to "
                f"'{compatible_sql_type}'."
            )
            raise NotImplementedError(msg)

        alter_column_ddl = self.get_column_alter_ddl(
            table_name=full_table_name,
            column_name=column_name,
            column_type=compatible_sql_type,
        )
        cursor.execute(alter_column_ddl)

    def get_column_alter_ddl(  # type: ignore[override]
        self,
        schema_name: str,
        table_name: str,
        column_name: str,
        column_type: TypeEngine,
    ) -> DDL:
        """Get the alter column DDL statement.

        Override this if your database uses a different syntax for altering columns.

        Args:
            schema_name: Schema name.
            table_name: Fully qualified table name of column to alter.
            column_name: Column name to alter.
            column_type: New column type string.

        Returns:
            A sqlalchemy DDL instance.
        """
        column = Column(column_name, column_type)
        return DDL(
            ('ALTER TABLE "%(schema_name)s"."%(table_name)s"' "ALTER COLUMN %(column_name)s %(column_type)s"),
            {
                "schema_name": schema_name,
                "table_name": table_name,
                "column_name": column.compile(dialect=self._engine.dialect),
                "column_type": column.type.compile(dialect=self._engine.dialect),
            },
        )

    def get_sqlalchemy_url(self, config: dict) -> str:
        """Generate a SQLAlchemy URL.

        Args:
            config: The configuration for the connector.
        """
        if config.get("sqlalchemy_url"):
            return cast(str, config["sqlalchemy_url"])

        else:
            sqlalchemy_url = URL.create(
                drivername=config["dialect+driver"],
                username=config["user"],
                password=config["password"],
                host=config["host"],
                port=config["port"],
                database=config["dbname"],
                query=self.get_sqlalchemy_query(config),
            )
            return cast(str, sqlalchemy_url)

    def get_sqlalchemy_query(self, config: dict) -> dict:
        """Get query values to be used for sqlalchemy URL creation.

        Args:
            config: The configuration for the connector.

        Returns:
            A dictionary with key-value pairs for the sqlalchemy query.
        """
        query = {}

        # ssl_enable is for verifying the server's identity to the client.
        if config["ssl_enable"]:
            ssl_mode = config["ssl_mode"]
            query.update({"sslmode": ssl_mode})
        return query


class NOTYPE(TypeDecorator):
    """Type to use when none is provided in the schema."""

    impl = TEXT
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """Return value as is unless it is dict or list.

        Used internally by SQL Alchemy. Should not be used directly.
        """
        if value is not None and isinstance(value, (dict, list)):
            value = simplejson.dumps(value, use_decimal=True)
        return value

    @property
    def python_type(self):
        """Return the Python type for this column."""
        return object

    def as_generic(self, *args: t.Any, **kwargs: t.Any):
        """Return the generic type for this column."""
        return VARCHAR()
