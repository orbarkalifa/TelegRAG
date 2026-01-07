import os
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from fastembed import TextEmbedding

# 1. Connect to Qdrant
# We use the hostname 'qdrant' because this script runs inside the Docker network
client = QdrantClient(host="qdrant", port=6333)

COLLECTION_NAME = "knowledge_base"

def run_ingest():
    print("🚀 Starting Ingestion...")

    # 2. Check if collection exists, if not create it
    if not client.collection_exists(COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=384, distance=Distance.COSINE),
        )
        print(f"✅ Created collection: {COLLECTION_NAME}")

    # 3. Read the file
    file_path = "/app/data/knowledge.txt"
    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        return

    with open(file_path, "r") as f:
        # Split by lines and remove empty ones
        documents = [line.strip() for line in f.readlines() if line.strip()]

    print(f"📖 Read {len(documents)} lines from file.")

    # 4. Embed the documents (Turn text into numbers)
    print("🧠 Generating embeddings (this might take a moment)...")
    embedding_model = TextEmbedding()
    # fastembed returns a generator, so we convert to list
    embeddings = list(embedding_model.embed(documents))

    # 5. Upload to Qdrant
    points = []
    for i, (doc, vector) in enumerate(zip(documents, embeddings)):
        points.append(PointStruct(
            id=i,
            vector=vector,
            payload={"text": doc}
        ))

    client.upsert(
        collection_name=COLLECTION_NAME,
        points=points
    )
    print(f"✅ Successfully uploaded {len(points)} points to Qdrant!")

if __name__ == "__main__":
    run_ingest()