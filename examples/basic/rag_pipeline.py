"""A simple example to demonstrate the usage of `BasicRAGPipeline`."""
import logging
import uuid

import dotenv

from sciphi_r2r.core import GenerationConfig, LoggingDatabaseConnection
from sciphi_r2r.embeddings import OpenAIEmbeddingProvider
from sciphi_r2r.llms import OpenAIConfig, OpenAILLM
from sciphi_r2r.main import load_config
from sciphi_r2r.pipelines import BasicRAGPipeline
from sciphi_r2r.vector_dbs import PGVectorDB, QdrantDB


class DemoRAGPipeline(BasicRAGPipeline):
    # Modifies `BasicRAGPipeline` run to return search_results and completion
    def run(self, query, filters={}, limit=10, search_only=False):
        """
        Runs the completion pipeline.
        """
        self.pipeline_run_id = uuid.uuid4()
        transformed_query = self.transform_query(query)
        search_results = self.search(transformed_query, filters, limit)
        if search_only:
            return search_results, None
        context = self.construct_context(search_results)
        prompt = self.construct_prompt(
            {"query": transformed_query, "context": context}
        )
        completion = self.generate_completion(prompt)
        return search_results, completion


if __name__ == "__main__":
    dotenv.load_dotenv()

    (
        api_config,
        logging_config,
        embedding_config,
        database_config,
        language_model_config,
        text_splitter_config,
    ) = load_config()

    query = "Is the answer to the question `What are the energy levels for a particle in a box?` contained in the search results shown below?"

    logger = logging.getLogger(logging_config["name"])
    logging.basicConfig(level=logging_config["level"])

    logger.debug("Starting the rag pipeline.")

    embeddings_provider = OpenAIEmbeddingProvider()
    embedding_model = embedding_config["model"]
    embedding_dimension = embedding_config["dimension"]
    embedding_batch_size = embedding_config["batch_size"]

    db = (
        QdrantDB()
        if database_config["vector_db_provider"] == "qdrant"
        else PGVectorDB()
    )
    collection_name = database_config["collection_name"]
    db.initialize_collection(collection_name, embedding_dimension)

    llm = OpenAILLM(OpenAIConfig())
    generation_config = GenerationConfig(
        model_name=language_model_config["model_name"],
        temperature=language_model_config["temperature"],
        top_p=language_model_config["top_p"],
        top_k=language_model_config["top_k"],
        max_tokens_to_sample=language_model_config["max_tokens_to_sample"],
        do_stream=language_model_config["do_stream"],
    )

    logging_database = LoggingDatabaseConnection("completion_demo_logs_v1")
    pipeline = DemoRAGPipeline(
        llm,
        generation_config,
        db=db,
        logging_database=logging_database,
        embedding_model=embedding_model,
        embeddings_provider=embeddings_provider,
    )

    search_results, completion_1 = pipeline.run(query, search_only=False)

    for result in search_results:
        logger.info("-" * 100)
        logger.info(f"Search Result:\n{result}")

    # To delete the primary Physics document from the collection
    db.filtered_deletion("document_id", "a9b92938-12e6-5ea4-b412-a4a1d4b48a0c")

    search_results, completion_2 = pipeline.run(query, search_only=False)

    logger.info("After Deletion: ")
    for result in search_results:
        logger.info("-" * 100)
        logger.info(f"Search Result:\n{result}")

    logger.info("-" * 100)
    logger.info(f"Completion 1 Result:\n{completion_1}")
    logger.info(f"Completion 2 Result:\n{completion_2}")
    logger.info("-" * 100)

    pipeline.close()