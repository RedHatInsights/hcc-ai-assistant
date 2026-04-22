"""
MCP Discovery Service - Python Implementation
Discovers and indexes MCP tools from configured servers with vector embeddings.
"""
import asyncio
import json
import logging
import os
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Dict, Any, Optional

import httpx
import yaml
from pydantic import BaseModel

# MCP SDK imports
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Import MCP server module
from mcp_server import MCPDiscoveryServer

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [mcp-discovery] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    """Service configuration from environment variables"""

    # Server configuration
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8001"))

    # MCP configuration
    MCP_CONFIG_PATH: str = os.getenv("MCP_CONFIG_PATH", "/app-root/lightspeed-stack.yaml")
    CAPABILITIES_CACHE_PATH: str = os.getenv("CAPABILITIES_CACHE_PATH", "/app-root/data/mcp-capabilities.json")
    REFRESH_INTERVAL_MINUTES: int = int(os.getenv("REFRESH_INTERVAL_MINUTES", "5"))

    # Vector store configuration
    ENABLE_VECTOR_STORE: bool = os.getenv("ENABLE_VECTOR_STORE", "").lower() in ("true", "1")
    EMBEDDING_SERVICE_URL: str = os.getenv("EMBEDDING_SERVICE_URL", "http://localhost:8002")
    VECTOR_STORE_ID: str = os.getenv("VECTOR_STORE_ID", "mcp-capabilities-store")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-mpnet-base-v2")

config = Config()

# ============================================================================
# DATA MODELS
# ============================================================================

class MCPServer(BaseModel):
    """MCP server configuration"""
    name: str
    provider_id: str
    url: str

class MCPCapability(BaseModel):
    """Discovered MCP tool capability"""
    server_name: str
    tool_name: str
    description: str
    input_schema: Dict[str, Any]

class MCPServersConfig(BaseModel):
    """MCP servers YAML configuration"""
    mcp_servers: List[MCPServer] = []  # Default to empty list if not provided

# ============================================================================
# VECTOR STORE CLIENT
# ============================================================================

class VectorStoreClient:
    """Client for interacting with embedding-service for embeddings and vector storage"""

    def __init__(self, base_url: str, vector_store_id: str, embedding_model: str):
        self.base_url = base_url.rstrip("/")
        self.vector_store_id = vector_store_id
        self.embedding_model = embedding_model
        self.client = httpx.AsyncClient(timeout=30.0)

    async def generate_embedding(self, text: str) -> List[float]:
        """Generate embedding for text"""
        try:
            response = await self.client.post(
                f"{self.base_url}/v1/embeddings",
                json={
                    "input": [text],
                    "model": self.embedding_model
                }
            )
            response.raise_for_status()
            data = response.json()
            return data["data"][0]["embedding"]
        except Exception as e:
            logger.error(f"Error generating embedding: {e}")
            raise

    # TODO: each sync track old capabilities, make new ones available - compare new to old and prune old tools (or else db will grow unbounded)
    async def insert_capabilities(self, capabilities: List[MCPCapability]) -> None:
        """Insert capabilities into vector store"""
        if not capabilities:
            return

        try:
            # Prepare texts for embedding
            texts = [
                f"{cap.tool_name}: {cap.description}. Server: {cap.server_name}"
                for cap in capabilities
            ]

            # Generate embeddings in batch
            response = await self.client.post(
                f"{self.base_url}/v1/embeddings",
                json={
                    "input": texts,
                    "model": self.embedding_model
                }
            )
            response.raise_for_status()
            embeddings_data = response.json()
            embeddings = [item["embedding"] for item in embeddings_data["data"]]

            # Prepare documents
            documents = [
                {
                    "document_id": f"{cap.server_name}::{cap.tool_name}",
                    "content": texts[i],
                    "metadata": {
                        "tool_name": cap.tool_name,
                        "server_name": cap.server_name,
                        "description": cap.description
                    }
                }
                for i, cap in enumerate(capabilities)
            ]

            # Insert into vector store
            response = await self.client.post(
                f"{self.base_url}/v1/vector_io/insert",
                json={
                    "vector_store_id": self.vector_store_id,
                    "documents": documents,
                    "embeddings": embeddings
                }
            )
            response.raise_for_status()
            logger.info(f"Successfully inserted {len(capabilities)} capabilities into vector store")

        except Exception as e:
            logger.error(f"Error inserting capabilities: {e}")
            raise

    async def search_similar(self, query: str, k: int = 10) -> List[MCPCapability]:
        """Search for similar capabilities using vector similarity"""
        try:
            # Generate query embedding
            query_embedding = await self.generate_embedding(query)

            # Query vector store
            response = await self.client.post(
                f"{self.base_url}/v1/vector_io/query",
                json={
                    "vector_store_id": self.vector_store_id,
                    "query": query_embedding,
                    "k": k
                }
            )
            response.raise_for_status()
            data = response.json()

            # Convert results back to MCPCapability
            capabilities = []
            for result in data["results"]:
                metadata = result["metadata"]
                capabilities.append(MCPCapability(
                    tool_name=metadata["tool_name"],
                    server_name=metadata["server_name"],
                    description=metadata["description"],
                    input_schema={}  # Not stored in vector DB
                ))

            return capabilities

        except Exception as e:
            logger.error(f"Error searching vectors: {e}")
            raise

    async def close(self):
        """Close the HTTP client"""
        await self.client.aclose()

# ============================================================================
# MCP INDEXER
# ============================================================================

class MCPIndexer:
    """Manages discovery and indexing of MCP capabilities"""

    def __init__(self):
        self.capabilities: List[MCPCapability] = []
        self.last_refresh_time: datetime = datetime.now()
        self.vector_store: Optional[VectorStoreClient] = None

        # Initialize vector store if enabled
        if config.ENABLE_VECTOR_STORE:
            logger.info("Vector store enabled - will attempt to use semantic search")
            try:
                self.vector_store = VectorStoreClient(
                    config.EMBEDDING_SERVICE_URL,
                    config.VECTOR_STORE_ID,
                    config.EMBEDDING_MODEL
                )
            except Exception as e:
                logger.warning(f"Failed to create vector store: {e}. Falling back to keyword search.")
                self.vector_store = None
        else:
            logger.info("Vector store disabled - using keyword search only")

    def load_config(self) -> MCPServersConfig:
        """Load MCP servers configuration from YAML"""
        try:
            with open(config.MCP_CONFIG_PATH, 'r') as f:
                data = yaml.safe_load(f)
                # Handle case where mcp_servers is None (all entries commented out)
                if data and data.get('mcp_servers') is None:
                    data['mcp_servers'] = []
                return MCPServersConfig(**data)
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            raise

    async def discover_capabilities(self, server: MCPServer) -> List[MCPCapability]:
        """Discover capabilities from an MCP server using MCP protocol"""
        try:
            logger.info(f"Connecting to MCP server: {server.name} ({server.url})")

            # Connect to MCP server using Streamable HTTP transport
            async with streamablehttp_client(server.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    # Initialize the session
                    await session.initialize()

                    # List available tools
                    tools_result = await session.list_tools()

                    capabilities = []
                    for tool in tools_result.tools:
                        capabilities.append(MCPCapability(
                            server_name=server.name,
                            tool_name=tool.name,
                            description=tool.description or "",
                            input_schema=tool.inputSchema
                        ))

                    logger.info(f"Found {len(capabilities)} capabilities from {server.name}")
                    return capabilities

        except Exception as e:
            logger.warning(f"Failed to discover capabilities from {server.name}: {e}")
            logger.debug(f"Full traceback:\n{traceback.format_exc()}")
            return []

    async def refresh_index(self) -> None:
        """Refresh the index by discovering all MCP servers"""
        logger.info("Refreshing MCP index...")

        try:
            mcp_config = self.load_config()
            logger.info(f"Found {len(mcp_config.mcp_servers)} MCP servers in config")

            all_capabilities = []

            # Discover capabilities from each server
            for server in mcp_config.mcp_servers:
                capabilities = await self.discover_capabilities(server)
                all_capabilities.extend(capabilities)

            self.capabilities = all_capabilities
            self.last_refresh_time = datetime.now()

            # Insert into vector store if enabled
            if all_capabilities and self.vector_store:
                logger.info("Indexing capabilities in vector store...")
                # Retry logic for initial insertion (embedding service may not be ready yet)
                max_retries = 3
                retry_delay = 2  # seconds
                for attempt in range(max_retries):
                    try:
                        await self.vector_store.insert_capabilities(all_capabilities)
                        logger.info("Successfully indexed capabilities in vector store")
                        break
                    except Exception as e:
                        if attempt < max_retries - 1:
                            logger.warning(f"Failed to insert capabilities (attempt {attempt + 1}/{max_retries}): {e}")
                            logger.info(f"Retrying in {retry_delay} seconds...")
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 2  # Exponential backoff
                        else:
                            logger.warning(f"Failed to insert capabilities after {max_retries} attempts: {e}")
                            logger.info("Will retry on next background refresh")

            # Save to disk
            await self.save_capabilities()

            logger.info(f"Index refresh complete. Total capabilities: {len(self.capabilities)}")

        except Exception as e:
            logger.error(f"Error refreshing index: {e}")
            raise

    async def save_capabilities(self) -> None:
        """Persist capabilities to disk as JSON cache"""
        try:
            os.makedirs(os.path.dirname(config.CAPABILITIES_CACHE_PATH), exist_ok=True)
            with open(config.CAPABILITIES_CACHE_PATH, 'w') as f:
                json.dump(
                    [cap.model_dump() for cap in self.capabilities],
                    f,
                    indent=2
                )
        except Exception as e:
            logger.warning(f"Failed to save capabilities cache to disk: {e}")

    def search_keyword(self, query: str, limit: int = 10) -> List[MCPCapability]:
        """Keyword-based search (fallback)"""
        query_lower = query.lower()
        results = []

        for cap in self.capabilities:
            if len(results) >= limit:
                break
            if (query_lower in cap.tool_name.lower() or
                query_lower in cap.description.lower() or
                query_lower in cap.server_name.lower()):
                results.append(cap)

        return results

    async def search(self, query: str, limit: int = 10) -> tuple[List[MCPCapability], str]:
        """Search with semantic search + keyword fallback"""
        results = []
        method = "keyword search"

        # Try semantic search first
        if self.vector_store:
            try:
                results = await self.vector_store.search_similar(query, limit)
                if results:
                    method = "semantic search"
                    logger.debug(f"Semantic search found {len(results)} results for query: '{query}'")
                else:
                    logger.info(f"Semantic search returned no results for query: '{query}', falling back to keyword search")
            except Exception as e:
                logger.warning(f"Semantic search failed, falling back to keyword search: {e}")
        else:
            logger.debug("Vector store not available, using keyword search")

        # Fall back to keyword search
        if not results:
            results = self.search_keyword(query, limit)
            logger.debug(f"Keyword search found {len(results)} results for query: '{query}'")

        return results, method

# Global indexer instance
indexer = MCPIndexer()

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

async def background_refresh():
    """Background task to refresh MCP capabilities periodically"""
    # Initial indexing
    logger.info("Performing initial MCP server discovery...")
    try:
        await indexer.refresh_index()
    except Exception as e:
        logger.warning(f"Initial indexing failed: {e}")

    # Periodic refresh
    while True:
        await asyncio.sleep(config.REFRESH_INTERVAL_MINUTES * 60)
        logger.info("Background refresh triggered...")
        try:
            await indexer.refresh_index()
        except Exception as e:
            logger.warning(f"Background refresh failed: {e}")

# ============================================================================
# MCP PROTOCOL SERVER
# ============================================================================

# Create MCP server instance (will be initialized after indexer is ready)
mcp_server_instance: Optional[MCPDiscoveryServer] = None

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

async def main_async():
    """Run MCP server with background refresh"""
    logger.info("Starting MCP Discovery Service (Python)...")
    logger.info(f"  - Config: {config.MCP_CONFIG_PATH}")
    logger.info(f"  - Refresh interval: {config.REFRESH_INTERVAL_MINUTES} minutes")
    logger.info(f"  - Vector store: {'enabled' if config.ENABLE_VECTOR_STORE else 'disabled'}")

    # Create MCP server
    global mcp_server_instance
    mcp_server_instance = MCPDiscoveryServer(indexer)

    # Get ASGI app with lifespan manager
    app = mcp_server_instance.create_app()

    # Start background refresh task
    refresh_task = asyncio.create_task(background_refresh())

    # Run with uvicorn
    logger.info(f"Starting MCP server on {config.HOST}:{config.PORT}")
    logger.info(f"  - MCP endpoint: http://{config.HOST}:{config.PORT}/mcp")
    logger.info(f"  - Health check: http://{config.HOST}:{config.PORT}/health")

    import uvicorn
    config_uv = uvicorn.Config(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level="info"
    )
    server = uvicorn.Server(config_uv)

    try:
        await server.serve()
    finally:
        refresh_task.cancel()
        if indexer.vector_store:
            await indexer.vector_store.close()

if __name__ == "__main__":
    asyncio.run(main_async())
