---
id: multimodal_rag
title: Multimodal RAG (Hybrid Retrieval + Observability Dashboard)
tech_stack: [Python, PyTorch, Hugging Face, Qdrant, Neo4j, FastAPI, Streamlit, OpenAI-compatible APIs, OCR, Whisper, RAGAS]
impact_metrics:
  - "~40% improvement in retrieval precision (RAGAS) over a dense-only baseline"
  - "Multimodal ingestion across PDF, PNG, MP3, JPEG, TXT in a single pipeline"
  - "Real-time pipeline-health observability dashboard with Streamlit"
role: Solo builder (personal project)
url: https://github.com/karthikmuppinidi/multimodal-rag
---

Designed an enterprise-style Multimodal RAG system using PyTorch and Hugging Face models with a Qdrant vector store and a Neo4j knowledge graph for hybrid retrieval plus semantic reranking, achieving roughly a 40% improvement in retrieval precision (RAGAS score) over a dense-only baseline.

Implemented a multimodal ingestion engine that processes PDF, PNG, MP3, JPEG, and TXT inputs via OCR, image captioning, and audio transcription. Wrapped the pipeline in a FastAPI backend with an OpenAI-compatible interface, and built a Streamlit observability dashboard that surfaces ingest throughput, retrieval latency, reranker hit-rate, and per-document RAGAS scores in real time. The dashboard layer is the project's "LLM observability" face: every retrieval and generation step is instrumented and visualised so regressions can be caught before they reach users.
