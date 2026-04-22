"""
Embedding Service - Unified Embedding + Vector Storage Service
Compatible with llama-stack's /v1/embeddings and /v1/vector_io/* APIs

Combines:
- Text embedding generation (sentence-transformers)
- Vector storage and similarity search (pgvector on PostgreSQL)
"""
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import List, Dict, Any

import psycopg2
import psycopg2.extras
import psycopg2.pool
from fastapi import FastAPI, HTTPException
from pgvector.psycopg2 import register_vector
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [embedding-service] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Global instances
embedding_model = None
db_pool = None

# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

# Embedding models
class EmbeddingRequest(BaseModel):
    input: List[str]
    model: str = "sentence-transformers/all-mpnet-base-v2"

class EmbeddingData(BaseModel):
    embedding: List[float]
    index: int

class EmbeddingResponse(BaseModel):
    data: List[EmbeddingData]
    model: str
    usage: dict = {"total_tokens": 0}

# Vector storage models
class VectorDocument(BaseModel):
    document_id: str
    content: str
    metadata: Dict[str, Any]

class InsertRequest(BaseModel):
    vector_store_id: str
    documents: List[VectorDocument]
    embeddings: List[List[float]]

class QueryRequest(BaseModel):
    vector_store_id: str
    query: List[float]
    k: int = 10

class QueryResult(BaseModel):
    document_id: str
    score: float
    content: str
    metadata: Dict[str, Any]

class QueryResponse(BaseModel):
    results: List[QueryResult]

# ============================================================================
# DATABASE HELPERS
# ============================================================================

VECTOR_DIMENSION = 768  # sentence-transformers/all-mpnet-base-v2

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS vector_stores (
    id TEXT PRIMARY KEY,
    dimension INTEGER NOT NULL DEFAULT 768,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vector_documents (
    id TEXT NOT NULL,
    store_id TEXT NOT NULL REFERENCES vector_stores(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    embedding vector(768) NOT NULL,
    PRIMARY KEY (id, store_id)
);

CREATE INDEX IF NOT EXISTS idx_vector_documents_store ON vector_documents(store_id);

CREATE INDEX IF NOT EXISTS idx_vector_documents_embedding ON vector_documents
    USING hnsw (embedding vector_cosine_ops);
"""


def get_db_config():
    """Build database connection config from environment variables."""
    config = {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "hcc-ai-assistant"),
        "user": os.getenv("PGUSER", "postgres"),
        "password": os.getenv("PGPASSWORD", ""),
    }
    ssl_mode = os.getenv("PGSSLMODE")
    if ssl_mode:
        config["sslmode"] = ssl_mode
    return config


def init_db():
    """Create connection pool and initialize schema."""
    global db_pool
    db_config = get_db_config()
    logger.info(f"Connecting to PostgreSQL at {db_config['host']}:{db_config['port']}/{db_config['dbname']}")

    db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=5,
        **db_config,
    )

    # Create extension and schema first, then register the vector type
    conn = db_pool.getconn()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.autocommit = False
        register_vector(conn)
        logger.info("pgvector schema initialized successfully")
    finally:
        db_pool.putconn(conn)


def get_conn():
    """Get a connection from the pool with pgvector type registered."""
    conn = db_pool.getconn()
    # register_vector is idempotent and cheap — ensures the vector type
    # adapter is available even on freshly created pool connections.
    try:
        register_vector(conn)
    except Exception:
        db_pool.putconn(conn)
        raise
    return conn


def put_conn(conn):
    """Return a connection to the pool."""
    db_pool.putconn(conn)


# ============================================================================
# APPLICATION LIFESPAN
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager - handles startup and shutdown.
    """
    global embedding_model

    # Startup
    logger.info("Starting Embedding Service...")

    # Load embedding model
    logger.info("Loading embedding model: sentence-transformers/all-mpnet-base-v2")
    try:
        embedding_model = SentenceTransformer('sentence-transformers/all-mpnet-base-v2')
        logger.info("Embedding model loaded successfully")
    except Exception as e:
        logger.error(f"Failed to load embedding model: {e}")
        raise

    # Initialize PostgreSQL + pgvector
    logger.info("Initializing pgvector database")
    try:
        init_db()
    except Exception as e:
        logger.error(f"Failed to initialize pgvector: {e}")
        raise

    logger.info("Embedding Service startup complete")

    yield

    # Shutdown
    logger.info("Shutting down Embedding Service...")
    if db_pool is not None:
        db_pool.closeall()
        logger.info("Database connection pool closed")
    logger.info("Embedding Service shutdown complete")


# Create FastAPI app with lifespan manager
app = FastAPI(
    title="Embedding Service",
    version="1.0.0",
    lifespan=lifespan
)

# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.get("/health")
async def health():
    """Health check endpoint"""
    db_ok = False
    if db_pool is not None:
        try:
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                db_ok = True
            finally:
                put_conn(conn)
        except Exception:
            pass

    return {
        "status": "healthy" if (embedding_model is not None and db_ok) else "degraded",
        "embedding_model_loaded": embedding_model is not None,
        "pgvector_connected": db_ok,
        "model": "sentence-transformers/all-mpnet-base-v2",
    }

# ============================================================================
# EMBEDDING ENDPOINTS
# ============================================================================

@app.post("/v1/embeddings", response_model=EmbeddingResponse)
async def create_embeddings(request: EmbeddingRequest):
    """
    Generate embeddings for input texts.
    Compatible with llama-stack's /v1/embeddings API.
    """
    if embedding_model is None:
        raise HTTPException(status_code=503, detail="Embedding model not loaded")

    if not request.input or len(request.input) == 0:
        raise HTTPException(status_code=400, detail="Input cannot be empty")

    try:
        logger.info(f"Generating embeddings for {len(request.input)} texts")

        # Generate embeddings
        embeddings = embedding_model.encode(request.input, convert_to_numpy=True)

        # Format response to match llama-stack API
        data = [
            EmbeddingData(
                embedding=embedding.tolist(),
                index=idx
            )
            for idx, embedding in enumerate(embeddings)
        ]

        return EmbeddingResponse(
            data=data,
            model=request.model,
            usage={"total_tokens": sum(len(text.split()) for text in request.input)}
        )

    except Exception as e:
        logger.error(f"Error generating embeddings: {e}")
        raise HTTPException(status_code=500, detail=f"Error generating embeddings: {str(e)}")

# ============================================================================
# VECTOR STORAGE ENDPOINTS
# ============================================================================

@app.post("/v1/vector_io/insert")
async def insert_vectors(request: InsertRequest):
    """
    Insert documents with their embeddings into a vector store.
    Compatible with llama-stack's /v1/vector_io/insert API.
    """
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    if len(request.documents) != len(request.embeddings):
        raise HTTPException(
            status_code=400,
            detail=f"Number of documents ({len(request.documents)}) must match number of embeddings ({len(request.embeddings)})"
        )

    try:
        logger.info(f"Inserting {len(request.documents)} documents into vector store: {request.vector_store_id}")

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Ensure the vector store exists
                dimension = len(request.embeddings[0]) if request.embeddings else VECTOR_DIMENSION
                cur.execute(
                    """INSERT INTO vector_stores (id, dimension)
                       VALUES (%s, %s)
                       ON CONFLICT (id) DO NOTHING""",
                    (request.vector_store_id, dimension),
                )

                # Upsert documents with embeddings
                for doc, emb in zip(request.documents, request.embeddings):
                    cur.execute(
                        """INSERT INTO vector_documents (id, store_id, content, metadata, embedding)
                           VALUES (%s, %s, %s, %s, %s::vector)
                           ON CONFLICT (id, store_id) DO UPDATE
                           SET content = EXCLUDED.content,
                               metadata = EXCLUDED.metadata,
                               embedding = EXCLUDED.embedding""",
                        (
                            doc.document_id,
                            request.vector_store_id,
                            doc.content,
                            json.dumps(doc.metadata),
                            str(emb),
                        ),
                    )

            conn.commit()
            logger.info(f"Successfully inserted {len(request.documents)} documents")
            return {"status": "success", "inserted": len(request.documents)}
        except Exception:
            conn.rollback()
            raise
        finally:
            put_conn(conn)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error inserting vectors: {e}")
        raise HTTPException(status_code=500, detail=f"Error inserting vectors: {str(e)}")

@app.post("/v1/vector_io/query", response_model=QueryResponse)
async def query_vectors(request: QueryRequest):
    """
    Query vector store for similar documents.
    Compatible with llama-stack's /v1/vector_io/query API.
    """
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    try:
        logger.info(f"Querying vector store: {request.vector_store_id} (k={request.k})")

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Check store exists
                cur.execute("SELECT 1 FROM vector_stores WHERE id = %s", (request.vector_store_id,))
                if cur.fetchone() is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Vector store not found: {request.vector_store_id}"
                    )

                # Cosine distance query — <=> returns values in [0, 2]
                cur.execute(
                    """SELECT id, content, metadata,
                              embedding <=> %s::vector AS distance
                       FROM vector_documents
                       WHERE store_id = %s
                       ORDER BY distance
                       LIMIT %s""",
                    (str(request.query), request.vector_store_id, request.k),
                )

                rows = cur.fetchall()
        finally:
            put_conn(conn)

        query_results = []
        for doc_id, content, metadata, distance in rows:
            # Convert cosine distance [0,2] to similarity score [0,1]
            score = 1.0 - (distance / 2.0)
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            query_results.append(QueryResult(
                document_id=doc_id,
                score=score,
                content=content,
                metadata=metadata,
            ))

        logger.info(f"Query returned {len(query_results)} results")
        return QueryResponse(results=query_results)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error querying vectors: {e}")
        raise HTTPException(status_code=500, detail=f"Error querying vectors: {str(e)}")

@app.delete("/v1/vector_io/store/{vector_store_id}")
async def delete_vector_store(vector_store_id: str):
    """Delete a vector store and all its documents (cascade)."""
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM vector_stores WHERE id = %s RETURNING id", (vector_store_id,))
                deleted = cur.fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            put_conn(conn)

        if deleted is None:
            raise HTTPException(status_code=404, detail=f"Vector store not found: {vector_store_id}")

        logger.info(f"Deleted vector store: {vector_store_id}")
        return {"status": "success", "deleted": vector_store_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting vector store: {e}")
        raise HTTPException(status_code=500, detail=f"Error deleting vector store: {str(e)}")

@app.get("/v1/vector_io/stores")
async def list_vector_stores():
    """List all vector stores with document counts."""
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT vs.id, vs.dimension, vs.created_at, COUNT(vd.id) AS doc_count
                       FROM vector_stores vs
                       LEFT JOIN vector_documents vd ON vs.id = vd.store_id
                       GROUP BY vs.id, vs.dimension, vs.created_at
                       ORDER BY vs.created_at"""
                )
                rows = cur.fetchall()
        finally:
            put_conn(conn)

        return {
            "stores": [
                {
                    "id": row[0],
                    "count": row[3],
                    "metadata": {"dimension": row[1]},
                }
                for row in rows
            ]
        }

    except Exception as e:
        logger.error(f"Error listing vector stores: {e}")
        raise HTTPException(status_code=500, detail=f"Error listing vector stores: {str(e)}")

# ============================================================================
# ROOT ENDPOINT
# ============================================================================

@app.get("/")
async def root():
    """Root endpoint with service info"""
    return {
        "service": "Embedding Service",
        "version": "1.0.0",
        "description": "Unified embedding generation and vector storage service",
        "model": "sentence-transformers/all-mpnet-base-v2",
        "vector_backend": "pgvector",
        "endpoints": {
            "health": "/health",
            "embeddings": "/v1/embeddings",
            "vector_insert": "/v1/vector_io/insert",
            "vector_query": "/v1/vector_io/query",
            "vector_list": "/v1/vector_io/stores",
            "vector_delete": "/v1/vector_io/store/{id}"
        }
    }
