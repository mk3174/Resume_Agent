---
id: clinical_rag_teksystems
title: Clinical RAG + Diagnostic ML (TEKsystems)
tech_stack: [Python, LangChain, PyTorch, TensorFlow, Delta Lake, AWS S3, Vector Retrieval, MLOps]
impact_metrics:
  - "~70% reduction in average medical record retrieval time vs manual lookup"
  - "+18% patient-outcome prediction accuracy over rule-based baseline (5k+ records)"
  - "40% faster model-related incident response via golden-set + HITL eval pipeline"
  - "10M+ clinical records processed with full auditability"
role: Associate Software Engineer
url: ""
---

Built production RAG pipelines and semantic-search systems at TEKsystems using LangChain with custom embeddings and vector retrieval to power context-aware clinical NLP responses, reducing average medical record retrieval time by ~70% vs manual lookup and directly improving clinician throughput.

Developed and deployed predictive diagnostic models in PyTorch and TensorFlow using multi-label classification and ensemble learning, improving patient-outcome prediction accuracy by 18% over the existing rule-based baseline (validated on a held-out clinical test set of 5,000+ records).

Designed MLOps-aligned evaluation pipelines with golden datasets, automated test suites, and human-in-the-loop validation, cutting model-related incident response time by 40% by catching accuracy and bias regressions before prod deployment. Engineered scalable ETL pipelines on Delta Lake + AWS S3 with metadata cataloguing, processing 10M+ clinical records with full auditability across the model development lifecycle.
