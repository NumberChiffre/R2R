import asyncio
import json
import logging
from typing import Any, Optional, Union
from uuid import UUID

import asyncpg

from core.base import (
    DocumentHandler,
    DocumentInfo,
    DocumentType,
    IngestionStatus,
    KGEnrichmentStatus,
    KGExtractionStatus,
    R2RException,
)

from .base import PostgresConnectionManager

logger = logging.getLogger()


class PostgresDocumentHandler(DocumentHandler):
    TABLE_NAME = "document_info"

    def __init__(
        self, project_name: str, connection_manager: PostgresConnectionManager
    ):
        super().__init__(project_name, connection_manager)

    async def create_tables(self):
        logger.info(
            f"Creating table, if not exists: {self._get_table_name(PostgresDocumentHandler.TABLE_NAME)}"
        )
        query = f"""
        CREATE TABLE IF NOT EXISTS {self._get_table_name(PostgresDocumentHandler.TABLE_NAME)} (
            document_id UUID PRIMARY KEY,
            collection_ids UUID[],
            user_id UUID,
            type TEXT,
            metadata JSONB,
            title TEXT,
            version TEXT,
            size_in_bytes INT,
            ingestion_status TEXT DEFAULT 'pending',
            kg_extraction_status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            ingestion_attempt_number INT DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_collection_ids_{self.project_name}
        ON {self._get_table_name(PostgresDocumentHandler.TABLE_NAME)} USING GIN (collection_ids);
        """
        await self.connection_manager.execute_query(query)

    async def upsert_documents_overview(
        self, documents_overview: Union[DocumentInfo, list[DocumentInfo]]
    ) -> None:
        if isinstance(documents_overview, DocumentInfo):
            documents_overview = [documents_overview]

        # TODO: make this an arg
        max_retries = 20
        for document_info in documents_overview:
            retries = 0
            while retries < max_retries:
                try:
                    async with self.connection_manager.pool.get_connection() as conn:  # type: ignore
                        async with conn.transaction():
                            # Lock the row for update
                            check_query = f"""
                            SELECT ingestion_attempt_number, ingestion_status FROM {self._get_table_name(PostgresDocumentHandler.TABLE_NAME)}
                            WHERE document_id = $1 FOR UPDATE
                            """
                            existing_doc = await conn.fetchrow(
                                check_query, document_info.id
                            )

                            db_entry = document_info.convert_to_db_entry()

                            if existing_doc:
                                db_version = existing_doc[
                                    "ingestion_attempt_number"
                                ]
                                db_status = existing_doc["ingestion_status"]
                                new_version = db_entry[
                                    "ingestion_attempt_number"
                                ]

                                # Only increment version if status is changing to 'success' or if it's a new version
                                if (
                                    db_status != "success"
                                    and db_entry["ingestion_status"]
                                    == "success"
                                ) or (new_version > db_version):
                                    new_attempt_number = db_version + 1
                                else:
                                    new_attempt_number = db_version

                                db_entry["ingestion_attempt_number"] = (
                                    new_attempt_number
                                )

                                update_query = f"""
                                UPDATE {self._get_table_name(PostgresDocumentHandler.TABLE_NAME)}
                                SET collection_ids = $1, user_id = $2, type = $3, metadata = $4,
                                    title = $5, version = $6, size_in_bytes = $7, ingestion_status = $8,
                                    kg_extraction_status = $9, updated_at = $10, ingestion_attempt_number = $11
                                WHERE document_id = $12
                                """
                                await conn.execute(
                                    update_query,
                                    db_entry["collection_ids"],
                                    db_entry["user_id"],
                                    db_entry["document_type"],
                                    db_entry["metadata"],
                                    db_entry["title"],
                                    db_entry["version"],
                                    db_entry["size_in_bytes"],
                                    db_entry["ingestion_status"],
                                    db_entry["kg_extraction_status"],
                                    db_entry["updated_at"],
                                    new_attempt_number,
                                    document_info.id,
                                )
                            else:
                                insert_query = f"""
                                INSERT INTO {self._get_table_name(PostgresDocumentHandler.TABLE_NAME)}
                                (document_id, collection_ids, user_id, type, metadata, title, version,
                                size_in_bytes, ingestion_status, kg_extraction_status, created_at,
                                updated_at, ingestion_attempt_number)
                                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                                """
                                await conn.execute(
                                    insert_query,
                                    db_entry["document_id"],
                                    db_entry["collection_ids"],
                                    db_entry["user_id"],
                                    db_entry["document_type"],
                                    db_entry["metadata"],
                                    db_entry["title"],
                                    db_entry["version"],
                                    db_entry["size_in_bytes"],
                                    db_entry["ingestion_status"],
                                    db_entry["kg_extraction_status"],
                                    db_entry["created_at"],
                                    db_entry["updated_at"],
                                    db_entry["ingestion_attempt_number"],
                                )

                    break  # Success, exit the retry loop
                except (
                    asyncpg.exceptions.UniqueViolationError,
                    asyncpg.exceptions.DeadlockDetectedError,
                ) as e:
                    retries += 1
                    if retries == max_retries:
                        logger.error(
                            f"Failed to update document {document_info.id} after {max_retries} attempts. Error: {str(e)}"
                        )
                        raise
                    else:
                        wait_time = 0.1 * (2**retries)  # Exponential backoff
                        await asyncio.sleep(wait_time)

    async def delete_from_documents_overview(
        self, document_id: UUID, version: Optional[str] = None
    ) -> None:
        query = f"""
        DELETE FROM {self._get_table_name(PostgresDocumentHandler.TABLE_NAME)}
        WHERE document_id = $1
        """

        params = [str(document_id)]

        if version:
            query += " AND version = $2"
            params = [str(document_id), version]

        await self.connection_manager.execute_query(query, params)

    async def _get_status_from_table(
        self,
        ids: list[UUID],
        table_name: str,
        status_type: str,
        column_name: str,
    ):
        """
        Get the workflow status for a given document or list of documents.

        Args:
            ids (list[UUID]): The document IDs.
            table_name (str): The table name.
            status_type (str): The type of status to retrieve.

        Returns:
            The workflow status for the given document or list of documents.
        """
        query = f"""
            SELECT {status_type} FROM {self._get_table_name(table_name)}
            WHERE {column_name} = ANY($1)
        """
        return await self.connection_manager.fetch_query(query, [ids])

    async def _get_ids_from_table(
        self,
        status: list[str],
        table_name: str,
        status_type: str,
        collection_id: Optional[UUID] = None,
    ):
        """
        Get the IDs from a given table.

        Args:
            status (Union[str, list[str]]): The status or list of statuses to retrieve.
            table_name (str): The table name.
            status_type (str): The type of status to retrieve.
        """
        query = f"""
            SELECT document_id FROM {self._get_table_name(table_name)}
            WHERE {status_type} = ANY($1) and $2 = ANY(collection_ids)
        """
        records = await self.connection_manager.fetch_query(
            query, [status, collection_id]
        )
        document_ids = [record["document_id"] for record in records]
        return document_ids

    async def _set_status_in_table(
        self,
        ids: list[UUID],
        status: str,
        table_name: str,
        status_type: str,
        column_name: str,
    ):
        """
        Set the workflow status for a given document or list of documents.

        Args:
            ids (list[UUID]): The document IDs.
            status (str): The status to set.
            table_name (str): The table name.
            status_type (str): The type of status to set.
            column_name (str): The column name in the table to update.
        """
        query = f"""
            UPDATE {self._get_table_name(table_name)}
            SET {status_type} = $1
            WHERE {column_name} = Any($2)
        """
        await self.connection_manager.execute_query(query, [status, ids])

    def _get_status_model(self, status_type: str):
        """
        Get the status model for a given status type.

        Args:
            status_type (str): The type of status to retrieve.

        Returns:
            The status model for the given status type.
        """
        if status_type == "ingestion":
            return IngestionStatus
        elif status_type == "kg_extraction_status":
            return KGExtractionStatus
        elif status_type == "kg_enrichment_status":
            return KGEnrichmentStatus
        else:
            raise R2RException(
                status_code=400, message=f"Invalid status type: {status_type}"
            )

    async def get_workflow_status(
        self, id: Union[UUID, list[UUID]], status_type: str
    ):
        """
        Get the workflow status for a given document or list of documents.

        Args:
            id (Union[UUID, list[UUID]]): The document ID or list of document IDs.
            status_type (str): The type of status to retrieve.

        Returns:
            The workflow status for the given document or list of documents.
        """
        ids = [id] if isinstance(id, UUID) else id
        out_model = self._get_status_model(status_type)
        result = list(
            map(
                await self._get_status_from_table(
                    ids,
                    out_model.table_name(),
                    status_type,
                    out_model.id_column(),
                ),
                out_model,
            )
        )
        return result[0] if isinstance(id, UUID) else result

    async def set_workflow_status(
        self, id: Union[UUID, list[UUID]], status_type: str, status: str
    ):
        """
        Set the workflow status for a given document or list of documents.

        Args:
            id (Union[UUID, list[UUID]]): The document ID or list of document IDs.
            status_type (str): The type of status to set.
            status (str): The status to set.
        """
        ids = [id] if isinstance(id, UUID) else id
        out_model = self._get_status_model(status_type)

        return await self._set_status_in_table(
            ids,
            status,
            out_model.table_name(),
            status_type,
            out_model.id_column(),
        )

    async def get_document_ids_by_status(
        self,
        status_type: str,
        status: Union[str, list[str]],
        collection_id: Optional[UUID] = None,
    ):
        """
        Get the IDs for a given status.

        Args:
            ids_key (str): The key to retrieve the IDs.
            status_type (str): The type of status to retrieve.
            status (Union[str, list[str]]): The status or list of statuses to retrieve.
        """

        if isinstance(status, str):
            status = [status]

        out_model = self._get_status_model(status_type)
        result = await self._get_ids_from_table(
            status, out_model.table_name(), status_type, collection_id
        )
        return result

    async def get_documents_overview(
        self,
        filter_user_ids: Optional[list[UUID]] = None,
        filter_document_ids: Optional[list[UUID]] = None,
        filter_collection_ids: Optional[list[UUID]] = None,
        offset: int = 0,
        limit: int = -1,
    ) -> dict[str, Any]:
        conditions = []
        params: list[Any] = []
        param_index = 1

        if filter_document_ids:
            conditions.append(f"document_id = ANY(${param_index})")
            params.append(filter_document_ids)
            param_index += 1

        if filter_user_ids:
            conditions.append(f"user_id = ANY(${param_index})")
            params.append(filter_user_ids)
            param_index += 1

        if filter_collection_ids:
            conditions.append(f"collection_ids && ${param_index}")
            params.append(filter_collection_ids)
            param_index += 1

        base_query = f"""
            FROM {self._get_table_name(PostgresDocumentHandler.TABLE_NAME)}
        """

        if conditions:
            base_query += " WHERE " + " AND ".join(conditions)

        query = f"""
            SELECT document_id, collection_ids, user_id, type, metadata, title, version,
                size_in_bytes, ingestion_status, kg_extraction_status, created_at, updated_at,
                COUNT(*) OVER() AS total_entries
            {base_query}
            ORDER BY created_at DESC
            OFFSET ${param_index}
        """
        params.append(offset)
        param_index += 1

        if limit != -1:
            query += f" LIMIT ${param_index}"
            params.append(limit)
            param_index += 1

        try:
            results = await self.connection_manager.fetch_query(query, params)
            total_entries = results[0]["total_entries"] if results else 0

            documents = [
                DocumentInfo(
                    id=row["document_id"],
                    collection_ids=row["collection_ids"],
                    user_id=row["user_id"],
                    document_type=DocumentType(row["type"]),
                    metadata=json.loads(row["metadata"]),
                    title=row["title"],
                    version=row["version"],
                    size_in_bytes=row["size_in_bytes"],
                    ingestion_status=IngestionStatus(row["ingestion_status"]),
                    kg_extraction_status=KGExtractionStatus(
                        row["kg_extraction_status"]
                    ),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in results
            ]

            return {"results": documents, "total_entries": total_entries}
        except Exception as e:
            logger.error(f"Error in get_documents_overview: {str(e)}")
            raise R2RException(
                status_code=500, message="Database query failed"
            )
