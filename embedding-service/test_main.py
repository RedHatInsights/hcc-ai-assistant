"""
Tests for Embedding Service
Uses pytest with modern best practices and fixtures for test setup.
"""
import json
import pytest
from unittest.mock import MagicMock, patch, call
import numpy as np
from httpx import AsyncClient, ASGITransport


# ============================================================================
# MOCK FIXTURES
# ============================================================================

@pytest.fixture
def mock_sentence_transformer():
    """Mock SentenceTransformer model"""
    mock_model = MagicMock()

    # Make encode return embeddings based on input length
    def dynamic_encode(texts, convert_to_numpy=True):
        embeddings = np.array([
            [0.1 * (i+1), 0.2 * (i+1), 0.3 * (i+1)]
            for i in range(len(texts))
        ])
        return embeddings

    mock_model.encode.side_effect = dynamic_encode
    return mock_model


@pytest.fixture
def mock_cursor():
    """Mock database cursor"""
    cursor = MagicMock()
    cursor.fetchone.return_value = (1,)
    cursor.fetchall.return_value = []
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    return cursor


@pytest.fixture
def mock_connection(mock_cursor):
    """Mock database connection"""
    conn = MagicMock()
    conn.cursor.return_value = mock_cursor
    return conn


@pytest.fixture
def mock_db_pool(mock_connection):
    """Mock database connection pool"""
    pool = MagicMock()
    pool.getconn.return_value = mock_connection
    return pool


@pytest.fixture
async def test_app(mock_sentence_transformer, mock_db_pool):
    """Create test FastAPI app with mocked dependencies"""
    with patch('main.SentenceTransformer', return_value=mock_sentence_transformer), \
         patch('main.psycopg2.pool.ThreadedConnectionPool', return_value=mock_db_pool), \
         patch('main.register_vector'):

        from main import app

        # Manually trigger startup to initialize global variables
        async with app.router.lifespan_context(app):
            yield app


@pytest.fixture
async def client(test_app):
    """Create async HTTP client for testing"""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ============================================================================
# TEST DATA FIXTURES
# ============================================================================

@pytest.fixture
def sample_embedding_request():
    """Sample embedding request data"""
    return {
        "input": ["hello world", "test text"],
        "model": "sentence-transformers/all-mpnet-base-v2"
    }


@pytest.fixture
def sample_vector_document():
    """Sample vector document"""
    return {
        "document_id": "doc1",
        "content": "test content",
        "metadata": {"key": "value"}
    }


@pytest.fixture
def sample_embedding():
    """Sample embedding vector"""
    return [0.1, 0.2, 0.3]


@pytest.fixture
def sample_insert_request(sample_vector_document, sample_embedding):
    """Sample vector insert request"""
    return {
        "vector_store_id": "test_store",
        "documents": [sample_vector_document],
        "embeddings": [sample_embedding]
    }


@pytest.fixture
def sample_query_request(sample_embedding):
    """Sample vector query request"""
    return {
        "vector_store_id": "test_store",
        "query": sample_embedding,
        "k": 2
    }


# ============================================================================
# HEALTH CHECK TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_health_endpoint(client):
    """Test health check endpoint returns correct status"""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["embedding_model_loaded"] is True
    assert data["pgvector_connected"] is True
    assert data["model"] == "sentence-transformers/all-mpnet-base-v2"


@pytest.mark.asyncio
async def test_root_endpoint(client):
    """Test root endpoint returns service info"""
    response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "Embedding Service"
    assert data["version"] == "1.0.0"
    assert data["vector_backend"] == "pgvector"
    assert "endpoints" in data


# ============================================================================
# EMBEDDING GENERATION TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_create_embeddings_success(client, sample_embedding_request):
    """Test successful embedding generation"""
    response = await client.post("/v1/embeddings", json=sample_embedding_request)
    assert response.status_code == 200

    data = response.json()
    assert len(data["data"]) == 2
    assert data["model"] == sample_embedding_request["model"]
    assert data["data"][0]["index"] == 0
    assert data["data"][1]["index"] == 1
    assert len(data["data"][0]["embedding"]) == 3
    assert data["usage"]["total_tokens"] > 0


@pytest.mark.asyncio
async def test_create_embeddings_empty_input(client):
    """Test embedding generation with empty input"""
    response = await client.post("/v1/embeddings", json={"input": []})
    assert response.status_code == 400
    assert "cannot be empty" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("input_texts,expected_count", [
    (["single text"], 1),
    (["text one", "text two", "text three"], 3),
    (["a" * 100], 1),  # Long text
])
async def test_create_embeddings_various_inputs(client, input_texts, expected_count):
    """Test embedding generation with various input sizes"""
    response = await client.post("/v1/embeddings", json={"input": input_texts})
    assert response.status_code == 200
    assert len(response.json()["data"]) == expected_count


# ============================================================================
# VECTOR STORAGE INSERT TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_insert_vectors_success(client, sample_insert_request):
    """Test successful vector insertion"""
    response = await client.post("/v1/vector_io/insert", json=sample_insert_request)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "success"
    assert data["inserted"] == 1


@pytest.mark.asyncio
async def test_insert_vectors_mismatch_count(client):
    """Test vector insertion with mismatched document and embedding counts"""
    request_data = {
        "vector_store_id": "test_store",
        "documents": [
            {"document_id": "doc1", "content": "test", "metadata": {}}
        ],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]]  # 2 embeddings for 1 document
    }

    response = await client.post("/v1/vector_io/insert", json=request_data)
    assert response.status_code == 400
    assert "must match" in response.json()["detail"]


@pytest.mark.asyncio
async def test_insert_vectors_multiple_documents(client):
    """Test inserting multiple documents at once"""
    request_data = {
        "vector_store_id": "test_store",
        "documents": [
            {"document_id": f"doc{i}", "content": f"content {i}", "metadata": {"index": i}}
            for i in range(5)
        ],
        "embeddings": [[0.1 * i, 0.2 * i, 0.3 * i] for i in range(5)]
    }

    response = await client.post("/v1/vector_io/insert", json=request_data)
    assert response.status_code == 200
    assert response.json()["inserted"] == 5


# ============================================================================
# VECTOR STORAGE QUERY TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_query_vectors_success(client, mock_db_pool, mock_connection, mock_cursor):
    """Test successful vector query"""
    # First call: check store exists -> return a row
    # Second call: cosine distance query -> return results
    mock_cursor.fetchone.return_value = (1,)
    mock_cursor.fetchall.return_value = [
        ("doc1", "test document 1", {"key": "value1"}, 0.2),
        ("doc2", "test document 2", {"key": "value2"}, 0.4),
    ]

    query_data = {
        "vector_store_id": "test_store",
        "query": [0.1, 0.2, 0.3],
        "k": 2
    }

    response = await client.post("/v1/vector_io/query", json=query_data)
    assert response.status_code == 200

    data = response.json()
    assert len(data["results"]) == 2
    assert data["results"][0]["document_id"] == "doc1"
    assert 0 < data["results"][0]["score"] <= 1


@pytest.mark.asyncio
async def test_query_vectors_nonexistent_store(client, mock_cursor):
    """Test querying a non-existent vector store"""
    mock_cursor.fetchone.return_value = None

    query_data = {
        "vector_store_id": "nonexistent_store",
        "query": [0.1, 0.2, 0.3],
        "k": 10
    }

    response = await client.post("/v1/vector_io/query", json=query_data)
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("k_value", [1, 5, 10, 20])
async def test_query_vectors_various_k_values(client, mock_cursor, k_value):
    """Test querying with various k values"""
    mock_cursor.fetchone.return_value = (1,)
    mock_cursor.fetchall.return_value = []

    query_data = {
        "vector_store_id": "test_store",
        "query": [0.1, 0.2, 0.3],
        "k": k_value
    }

    response = await client.post("/v1/vector_io/query", json=query_data)
    assert response.status_code == 200


# ============================================================================
# VECTOR STORE MANAGEMENT TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_list_vector_stores(client, mock_cursor):
    """Test listing all vector stores"""
    mock_cursor.fetchall.return_value = [
        ("store1", 768, "2024-01-01", 5),
        ("store2", 768, "2024-01-02", 10),
    ]

    response = await client.get("/v1/vector_io/stores")
    assert response.status_code == 200

    data = response.json()
    assert "stores" in data
    assert isinstance(data["stores"], list)
    assert len(data["stores"]) == 2
    assert data["stores"][0]["id"] == "store1"
    assert data["stores"][0]["count"] == 5


@pytest.mark.asyncio
async def test_delete_vector_store_success(client, mock_cursor):
    """Test successful vector store deletion"""
    store_id = "test_store_to_delete"
    mock_cursor.fetchone.return_value = (store_id,)

    response = await client.delete(f"/v1/vector_io/store/{store_id}")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "success"
    assert data["deleted"] == store_id


@pytest.mark.asyncio
async def test_delete_vector_store_nonexistent(client, mock_cursor):
    """Test deleting a non-existent vector store"""
    mock_cursor.fetchone.return_value = None

    response = await client.delete("/v1/vector_io/store/nonexistent_store")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()
