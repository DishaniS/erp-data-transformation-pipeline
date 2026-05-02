# ERP-Aware Data Transformation Pipeline

## 📌 Overview
This project implements an ERP-Aware Data Transformation Pipeline designed to convert legacy ERP data and unstructured documents into a unified AI-ready knowledge layer.

The system extracts structured ERP data, processes documents (PDFs/images), transforms them into semantic representations, and stores them using a hybrid tiered storage architecture to optimize cost and performance.

---

## 🎯 Objectives
- Transform legacy ERP data into AI-ready structured formats
- Reduce repeated AI computation through one-time document processing
- Enable semantic search using vector embeddings
- Implement a cost-efficient hybrid storage architecture

---

## 🏗️ System Workflow
1. Data Extraction from ERP Database
2. Data Cleaning and Normalization
3. Document Processing (PDF/OCR)
4. ERP-Aware Data Transformation
5. Semantic Embedding Generation
6. Hybrid Tiered Storage (Hot/Warm/Cold)

---

## 📂 Dataset
This project uses the AdventureWorksLT2019 dataset as the primary ERP data source.

### Tables Used:
- Customer
- Product
- ProductCategory
- ProductModel
- SalesOrderHeader
- SalesOrderDetail
- Address

---

## ⚙️ Tech Stack
- Python
- Pandas
- SQL Server / PostgreSQL
- FastAPI
- Sentence Transformers
- Qdrant / FAISS
- PyMuPDF / Tesseract OCR

---

## 🚀 Project Status
🔹 Stage 0: Dataset Setup — Completed  
🔹 Stage 1: Data Extraction — In Progress  

---

## 📌 Future Work
- Real-time data pipeline (CDC/Kafka)
- Advanced embedding models
- Optimization of hybrid storage
- Full API integration for AI query systems

---

## 👤 Author
Dishani