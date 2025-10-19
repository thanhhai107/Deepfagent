# Medicagent

Trợ lý y tế hiện đại sử dụng kiến trúc đa tác tử

## Features

- Giao diện trò chuyện y tế với nhiều tác tử (multi-agent), hỗ trợ truy xuất và tìm kiếm thông tin.
- Phân tích hình ảnh y tế
- Hỗ tương tác bằng giọng nói
- Xác minh của con người (Human Validation)

## Technologies

**Frontend:** Next.js 14, TypeScript, Tailwind CSS, shadcn/ui  
**Backend:** FastAPI, Python 3.12+, Pytorch, Langchain, LangGraph, Qdrant

| Component | Technologies |
|-----------|-------------|
|Agent Orchestration | LangGraph |
|Document Parsing | Docling |
|Knowledge Storage | Qdrant Vector Database |
|Conversation Model | GPT-4o (OpenAI) |
|Medical Imaging | Computer Vision Models:<br>• Brain Tumor: Object Detection (PyTorch)<br>• Chest X-ray: Image Classification (PyTorch)<br>• Skin Lesion: Semantic Segmentation (PyTorch) |
|Information Searching | Tavily API |
|Guardrails | LangChain |
|Speech Processing | Azure Speech |


## Conversation Agents

Hệ thống hỗ trợ nhiều tác nhân hội thoại chuyên biệt phục vụ cho các nhu cầu  khác nhau:

**Conversation Agent**  
  Tác nhân hỗ trợ hội thoại tổng quát.

**Medical RAG Agent**  
  Tác nhân truy xuất thông tin y khoa từ tài liệu và tri thức:  
  • Phân tích tài liệu PDF dựa trên Docling  
  • Xử lý và nhúng nội dung định dạng Markdown  
  • Sematic chunking
  • Tìm kiếm kết hợp với cơ sở dữ liệu vector Qdrant  

**Web Search Agent**  
  Tác nhân tìm kiếm thông tin y học từ internet:  
  • Tìm kiếm tài liệu nghiên cứu y học thông qua PubMed  
  • Tìm kiếm đa nguồn thông minh qua Tavily API

## Medical Vision Agents

Nhiều mô hình thị giác máy tính đã được tích hợp để hỗ trợ phân tích hình ảnh y tế chuyên sâu:

**Brain Tumor Agent**  
  • Phân loại hình ảnh MRI não  
  • Độ chính xác: 97.56%

**Chest X-ray Agent**  
  • Nhận diện Covid-19 từ ảnh X-quang ngực  
  • Độ chính xác: 97%

**Skin Lesion Agent**  
  • Phân vùng tổn thương da trên hình ảnh  
  • Dice Score: 0.784

# Environment Setup
Create a new virtual environment : https://www.anaconda.com/download
```bash
conda create --name medicagent python=3.12
conda activate medicagent
```
Download NodeJs from : https://nodejs.org/en/download


# Backend Setup
```bash
cd backend
python -m pip install -r requirements.txt
```
# Frontend Setup
```bash
cd frontend
npm install
```
# Run the application
```bash
cd backend
uvicorn app:app --reload
```
```bash
cd frontend
npm run dev
```