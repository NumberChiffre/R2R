import json
import logging
from typing import Any, AsyncGenerator, Optional
from uuid import UUID

from core.base import (
    AsyncState,
    CompletionProvider,
    DatabaseProvider,
    EmbeddingProvider,
)
from core.base.abstractions import (
    KGCommunityResult,
    KGEntityResult,
    KGSearchMethod,
    KGSearchResult,
    KGSearchResultType,
    KGSearchSettings,
)
from core.providers.logger.r2r_logger import SqlitePersistentLoggingProvider

from ..abstractions.generator_pipe import GeneratorPipe

logger = logging.getLogger()


class KGSearchSearchPipe(GeneratorPipe):
    """
    Embeds and stores documents using a specified embedding model and database.
    """

    def __init__(
        self,
        llm_provider: CompletionProvider,
        database_provider: DatabaseProvider,
        embedding_provider: EmbeddingProvider,
        config: GeneratorPipe.PipeConfig,
        logging_provider: SqlitePersistentLoggingProvider,
        *args,
        **kwargs,
    ):
        """
        Initializes the embedding pipe with necessary components and configurations.
        """
        super().__init__(
            llm_provider,
            database_provider,
            config,
            logging_provider,
            *args,
            **kwargs,
        )
        self.database_provider = database_provider
        self.llm_provider = llm_provider
        self.embedding_provider = embedding_provider
        self.pipe_run_info = None

    def filter_responses(self, map_responses):
        filtered_responses = []
        for response in map_responses:
            try:
                parsed_response = json.loads(response)
                for item in parsed_response["points"]:
                    try:
                        if item["score"] > 0:
                            filtered_responses.append(item)
                    except KeyError:
                        # Skip this item if it doesn't have a 'score' key
                        logger.warning(f"Item in response missing 'score' key")
                        continue
            except json.JSONDecodeError:
                logger.warning(
                    f"Response is not valid JSON: {response[:100]}..."
                )
                continue
            except KeyError:
                logger.warning(
                    f"Response is missing 'points' key: {response[:100]}..."
                )
                continue

        filtered_responses = sorted(
            filtered_responses, key=lambda x: x["score"], reverse=True
        )

        responses = "\n".join(
            [
                response.get("description", "")
                for response in filtered_responses
            ]
        )
        return responses

    async def local_search(
        self,
        input: GeneratorPipe.Input,
        state: AsyncState,
        run_id: UUID,
        kg_search_settings: KGSearchSettings,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncGenerator[KGSearchResult, None]:
        # search over communities and
        # do 3 searches. One over entities, one over relationships, one over communities

        async for message in input.message:
            query_embedding = (
                await self.embedding_provider.async_get_embedding(message)
            )

            # entity search
            search_type = "__Entity__"
            async for search_result in await self.database_provider.vector_query(  # type: ignore
                message,
                search_type=search_type,
                search_type_limits=kg_search_settings.local_search_limits[
                    search_type
                ],
                query_embedding=query_embedding,
                property_names=[
                    "name",
                    "description",
                    "extraction_ids",
                ],
                filters=kg_search_settings.filters,
                entities_level=kg_search_settings.entities_level,
            ):
                yield KGSearchResult(
                    content=KGEntityResult(
                        name=search_result["name"],
                        description=search_result["description"],
                    ),
                    method=KGSearchMethod.LOCAL,
                    result_type=KGSearchResultType.ENTITY,
                    extraction_ids=search_result["extraction_ids"],
                    metadata={"associated_query": message},
                )

            # relationship search
            # disabled for now. We will check evaluations and see if we need it
            # search_type = "__Relationship__"
            # async for search_result in self.database_provider.vector_query(  # type: ignore
            #     input,
            #     search_type=search_type,
            #     search_type_limits=kg_search_settings.local_search_limits[
            #         search_type
            #     ],
            #     query_embedding=query_embedding,
            #     property_names=[
            #         "name",
            #         "description",
            #         "extraction_ids",
            #         "document_ids",
            #     ],
            # ):
            #     yield KGSearchResult(
            #         content=KGRelationshipResult(
            #             name=search_result["name"],
            #             description=search_result["description"],
            #         ),
            #         method=KGSearchMethod.LOCAL,
            #         result_type=KGSearchResultType.RELATIONSHIP,
            #         # extraction_ids=search_result["extraction_ids"],
            #         # document_ids=search_result["document_ids"],
            #         metadata={"associated_query": message},
            #     )

            # community search
            search_type = "__Community__"
            async for search_result in await self.database_provider.vector_query(  # type: ignore
                message,
                search_type=search_type,
                search_type_limits=kg_search_settings.local_search_limits[
                    search_type
                ],
                embedding_type="embedding",
                query_embedding=query_embedding,
                property_names=[
                    "community_number",
                    "name",
                    "findings",
                    "rating",
                    "rating_explanation",
                    "summary",
                ],
                filters=kg_search_settings.filters,
            ):
                yield KGSearchResult(
                    content=KGCommunityResult(
                        name=search_result["name"],
                        summary=search_result["summary"],
                        rating=search_result["rating"],
                        rating_explanation=search_result["rating_explanation"],
                        findings=search_result["findings"],
                    ),
                    method=KGSearchMethod.LOCAL,
                    result_type=KGSearchResultType.COMMUNITY,
                    metadata={
                        "associated_query": message,
                    },
                )

    async def _run_logic(  # type: ignore
        self,
        input: GeneratorPipe.Input,
        state: AsyncState,
        run_id: UUID,
        kg_search_settings: KGSearchSettings,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncGenerator[KGSearchResult, None]:
        kg_search_type = kg_search_settings.kg_search_type

        if kg_search_type == "local":
            logger.info("Performing KG local search")
            async for result in self.local_search(
                input, state, run_id, kg_search_settings
            ):
                yield result
        else:
            raise ValueError(f"Unsupported KG search type: {kg_search_type}")
