# tools/memory_manager.py
# Adaptado para usar el servidor llama.cpp (CPU/GPU) que ya tienes en app_data

import os
import sys
import json
import uuid
import time
import requests
from typing import List, Dict, Any, Optional

import chromadb
from chromadb.config import Settings

class MemoryManager:
    """
    Gestor de memoria semántica persistente (ChromaDB).
    Usa el servidor de embeddings de llama.cpp (Nomic-Embed) que corre en local.
    """
    def __init__(self, persist_directory: Optional[str] = None, config: Optional[dict] = None):
        # 1. Directorio de persistencia (user_data/chroma_db)
        if persist_directory is None:
            if getattr(sys, 'frozen', False):
                base_dir = os.path.dirname(sys.executable)
            else:
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            persist_directory = os.path.join(base_dir, 'user_data', 'chroma_db')

        # 2. Configuración del servidor de embeddings (desde user_data/config.json)
        self.config = config or {}
        # URL base de tu llama-server (por defecto 8082, el mismo que usas para RAG)
        self.server_url = self.config.get('embedding_url', 'http://localhost:8082')
        # El endpoint de embeddings en llama.cpp es compatible con OpenAI
        self.embeddings_endpoint = f"{self.server_url}/v1/embeddings"
        # El modelo no importa mucho, pero lo ponemos para compatibilidad
        self.model_name = self.config.get('embedding_model', 'nomic-embed')

        # 3. Inicializar ChromaDB
        os.makedirs(persist_directory, exist_ok=True)
        self.client = chromadb.PersistentClient(
            path=persist_directory,
            settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection("agent_memory")

        # 4. Verificar que el servidor esté activo
        self.server_available = self._check_server()
        if not self.server_available:
            # Podríamos lanzar error, pero mejor advertir y permitir reintentos
            print("[Memory] ⚠️  ADVERTENCIA: No se pudo conectar al servidor de embeddings en", self.server_url)
            print("[Memory] Asegúrate de que llama-server (CPU/GPU) esté ejecutándose con el modelo Nomic.")
        else:
            print(f"[Memory] ✅ Conectado al servidor de embeddings: {self.server_url}")

    def _check_server(self) -> bool:
        """Verifica que llama-server esté respondiendo (usando el endpoint /health o /v1/models)."""
        try:
            # llama.cpp suele responder al endpoint /health o /v1/models
            # Probamos con /v1/models primero (estándar OpenAI)
            response = requests.get(f"{self.server_url}/v1/models", timeout=2)
            if response.status_code == 200:
                return True
            # Si no, probamos el health check de llama.cpp
            response = requests.get(f"{self.server_url}/health", timeout=2)
            return response.status_code == 200
        except:
            return False

    def _get_embedding(self, text: str) -> List[float]:
        """
        Llama al servidor llama.cpp para obtener el embedding de un texto.
        Formato: POST /v1/embeddings
        Body: {"input": "texto", "model": "nomic-embed"}
        """
        # llama.cpp requiere que el texto esté en una lista para 'input' (si es batch) o string.
        # Usamos string directamente, funciona.
        payload = {
            "input": text,
            "model": self.model_name
        }
        headers = {
            "Content-Type": "application/json"
        }
        try:
            response = requests.post(
                self.embeddings_endpoint,
                json=payload,
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            # El formato de respuesta de llama.cpp es: {"data": [{"embedding": [...]}]}
            if 'data' in data and len(data['data']) > 0:
                return data['data'][0]['embedding']
            else:
                raise ValueError("Respuesta del servidor sin datos de embedding")
        except Exception as e:
            print(f"[Memory] ❌ Error obteniendo embedding: {e}")
            # Si el servidor falla, podríamos reintentar, pero lo dejamos claro
            raise RuntimeError(f"Error al obtener embedding del servidor {self.server_url}. Asegúrate de que el servidor esté corriendo.") from e

    # --- Métodos Públicos (idénticos a antes) ---

    def add_memory(self, text: str, metadata: Dict[str, Any] = None, doc_id: str = None) -> str:
        """Almacena un texto en la memoria semántica."""
        if metadata is None:
            metadata = {}
        if doc_id is None:
            doc_id = str(uuid.uuid4())

        embedding = self._get_embedding(text)
        self.collection.add(
            ids=[doc_id],
            embeddings=[embedding],
            metadatas=[metadata],
            documents=[text]
        )
        return doc_id

    def search_memory(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Busca los textos más relevantes para la consulta."""
        if not query or query.strip() == "":
            return []

        query_embedding = self._get_embedding(query)
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=['documents', 'metadatas', 'distances']
        )

        memories = []
        if results['documents'] and len(results['documents']) > 0:
            for i, doc in enumerate(results['documents'][0]):
                memories.append({
                    'document': doc,
                    'metadata': results['metadatas'][0][i] if results['metadatas'] else {},
                    'distance': results['distances'][0][i] if results['distances'] else None,
                    'id': results['ids'][0][i] if results['ids'] else None
                })
        return memories

    def delete_memory(self, doc_id: str):
        self.collection.delete(ids=[doc_id])

    def clear_all(self):
        self.client.delete_collection("agent_memory")
        self.collection = self.client.get_or_create_collection("agent_memory")