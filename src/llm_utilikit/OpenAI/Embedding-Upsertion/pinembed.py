"""Easily automate the retrieval from OpenAI and storage of embeddings in Pinecone."""

import os
import logging
import asyncio
from dotenv import load_dotenv
from datetime import datetime
from typing import Dict, Union, List
import openai  
import pinecone  
import backoff  
from langchain.document_loaders import UnstructuredFileLoader

# Load environment variables from .env file
load_dotenv()

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class EnvConfig:
    """Class for handling environment variables and API keys."""
    
    def __init__(self) -> None:
        """Initialize environment variables."""
        self._openai_key: str = os.getenv("OPENAI_API_KEY")
        self._pinecone_key: str = os.getenv("PINECONE_API_KEY")
        self._pinecone_environment: str = os.getenv("PINECONE_ENVIRONMENT")
        self._pinecone_index: str = os.getenv("PINEDEX")
        self._drop_columns: List[str] = os.getenv("DROPCOLUMNS", "").split(",")
        
        self._drop_columns = [col.strip() for col in self._drop_columns if col.strip()]

    @property
    def openai_key(self) -> str:
        return self._openai_key

    @property
    def pinecone_key(self) -> str:
        return self._pinecone_key

    @property
    def pinecone_environment(self) -> str:
        return self._pinecone_environment

    @property
    def pinecone_index(self) -> str:
        return self._pinecone_index

    @property
    def drop_columns(self) -> List[str]:
        return self._drop_columns

class OpenAIHandler:
    """Class for handling OpenAI operations."""

    def __init__(self, config: EnvConfig) -> None:
        """Initialize OpenAI API key."""
        self.config = config

    async def __aenter__(self) -> "OpenAIHandler":
        openai.api_key = self.config.openai_key
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass
    
    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    async def create_embedding(self, input_text: str) -> Dict[str, Union[int, List[float]]]:
        """
        Create an embedding using OpenAI.
        
        Parameters:
            input_text (str): The text to be embedded.
            
        Returns:
            Dict[str, Union[int, List[float]]]: The embedding response.
        """
        try:
            async with self:
                response = openai.Embedding.create(
                    model="text-embedding-ada-002",
                    input=input_text,
                )
            if "data" not in response or not isinstance(response["data"], list):
                raise ValueError("Invalid embedding response format")
            embedding_data = response["data"][0]
            if "embedding" not in embedding_data:
                raise ValueError("Missing 'embedding' in response")
            return embedding_data["embedding"]
        except Exception as e:
            logger.error(f"Error creating embedding: {e}")
            raise

class PineconeHandler:
    """Class for handling Pinecone operations."""

    def __init__(self, config: EnvConfig) -> None:
        """Initialize Pinecone API key, environment, and index name."""
        self.config = config

    async def __aenter__(self) -> "PineconeHandler":
        pinecone.init(api_key=self.config.pinecone_key, environment=self.config.pinecone_environment)
        self.index = pinecone.Index(self.config.pinecone_index)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.index.deinit()

    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    async def upload_embedding(self, embedding: Dict[str, Union[int, List[float]]]) -> None:
        """
        Asynchronously uploads an embedding to Pinecone.
        
        Args:
            embedding (Dict): A dictionary containing the embedding data.
        """
        try:
            async with self:
                # Prepare the item for upsert
                item = {
                    'id': embedding['id'],
                    'values': embedding['values'],
                    'metadata': embedding.get('metadata', {}),
                    'sparse_values': embedding.get('sparse_values', {})
                }
                required_keys = ["id", "values"]
                if not all(key in embedding for key in required_keys):
                    raise ValueError(f"Embedding must contain the following keys: {required_keys}")
                self.index.upsert(vectors=[{'id': embedding['id'], 'values': embedding['values']}])
        except Exception as e:
            logger.error(f"Error uploading embedding: {e}")
            raise

class TextDataStreamHandler:
    """Class for handling text data streams."""

    def __init__(self, openai_handler: OpenAIHandler, pinecone_handler: PineconeHandler, data_dir: str = "data") -> None:
        """Initialize TextDataStreamHandler."""
        self.openai_handler = openai_handler
        self.pinecone_handler = pinecone_handler
        self.data_dir = data_dir
        self.last_run_time: datetime = datetime.now()
        self.lock = asyncio.Lock()
        self.queue = asyncio.Queue()
        self.event = asyncio.Event()

    async def process_data(self, filename: str) -> None:
        """
        Process data to create and upload embeddings.
        
        Parameters:
            filename (str): The name of the file to be processed.
        """
        file_path = os.path.join(self.data_dir, filename)
        if not os.path.isfile(file_path):
            logger.warning(f"File not found: {file_path}")
            return

        try:
            loader = UnstructuredFileLoader(self.data_dir)
            docs = loader.load(file_path)
            data = ""
            for doc in docs:
                data += doc.page_content
            
            async with self.openai_handler, self.pinecone_handler, self.lock:
                current_time = await asyncio.to_thread(datetime.now)
                elapsed_time = (current_time - self.last_run_time).total_seconds()
                if elapsed_time < 0.3:
                    await asyncio.sleep(0.3 - elapsed_time)
                
                self.last_run_time = current_time
                embedding = await self.openai_handler.create_embedding(data)
                if not isinstance(embedding, dict) or not all(key in embedding for key in ["id", "values"]):
                    raise ValueError("Invalid embedding format")
                await self.queue.put(embedding)
                self.event.set()
        except Exception as e:
            logger.error(f"Error processing data: {e}")

async def process_data_streams(data_streams: List[TextDataStreamHandler], filenames: List[str]) -> None:
    """
    Process data streams to create and upload embeddings.

    Parameters:
        data_streams (List[TextDataStreamHandler]): A list of TextDataStreamHandler instances.
        filenames (List[str]): A list of filenames to be processed.
    """
    tasks = []
    for filename in filenames:
        if filename.endswith((".pdf", ".txt")):
            for stream in data_streams:
                task = asyncio.create_task(stream.process_data(filename))
                tasks.append(task)
            await asyncio.sleep(0)
    await asyncio.gather(*tasks)

async def upload_embeddings(pinecone_handler: PineconeHandler, queue: asyncio.Queue, event: asyncio.Event) -> None:
    """
    Upload embeddings to Pinecone.

    Parameters:
        pinecone_handler (PineconeHandler): An instance of the PineconeHandler class.
        queue (asyncio.Queue): A queue containing embeddings to be uploaded.
        event (asyncio.Event): An event to signal when embeddings are available in the queue.
    """
    while True:
        await event.wait()
        embeddings = []
        while not queue.empty():
            embeddings.append(await queue.get())
        try:
            async with pinecone_handler:
                tasks = []
                for embedding in embeddings:
                    task = asyncio.create_task(pinecone_handler.upload_embedding(embedding))
                    tasks.append(task)
                await asyncio.gather(*tasks)
        except Exception as e:
            logger.error(f"Error uploading embeddings: {e}")
        finally:
            event.clear()

async def main() -> None:
    """
    The main function orchestrates the entire process of creating and uploading embeddings.

    It initializes the necessary handlers, prepares the data streams, and starts the asynchronous tasks
    for processing the data and uploading the embeddings.
    """
    try:
        config = EnvConfig()
        openai_handler = OpenAIHandler(config)
        pinecone_handler = PineconeHandler(config)
        data_streams = [TextDataStreamHandler(openai_handler, pinecone_handler) for _ in range(3)]
        filenames = [entry.name for entry in os.scandir("data") if entry.is_file()]
        upload_task = asyncio.create_task(upload_embeddings(pinecone_handler, data_streams[0].queue, data_streams[0].event))
        await asyncio.gather(process_data_streams(data_streams, filenames), upload_task)
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    asyncio.run(main())
