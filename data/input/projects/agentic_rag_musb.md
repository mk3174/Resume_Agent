---
id: agentic_rag_musb
title: Enterprise Agentic RAG (MusB Research)
tech_stack: [Python, LangGraph, FastAPI, Next.js, OpenAI API, Anthropic API, Prometheus, Grafana, SSE/Streaming]
impact_metrics:
  - "Cut analyst turnaround time by over 85% for 10k+ businesses"
  - "Scaled to 2k+ daily LLM-powered conversations with p95 latency under 800ms"
  - "Reduced mean time to detect production incidents by 60% via Prometheus + Grafana"
role: Founding Software Engineer
url: ""
---

Architected and deployed an enterprise-grade agentic RAG system at MusB Research with multi-step orchestration (tool use, memory, planning) using LangGraph and OpenAI-compatible LLM APIs. Replaced hours of manual portfolio analysis for 10k+ businesses with sub-minute, context-rich insights, cutting analyst turnaround time by over 85%.

Built a high-concurrency inference backend with FastAPI (SSE / streaming) and a Next.js client, scaling to 2k+ daily LLM-powered conversations at p95 under 800ms. Integrated Prometheus and Grafana dashboards that reduced mean time to detect production incidents by 60%. Containerised every service with Docker + Kubernetes and CI/CD, holding 99.9% uptime while cutting average deploy cycles from 2 hours to under 15 minutes.
