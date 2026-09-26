FROM python:3.11-slim

WORKDIR /app

COPY requirements-streamlit.txt .
RUN pip install --no-cache-dir -r requirements-streamlit.txt

COPY streamlit.py .

RUN useradd --create-home appuser
USER appuser

ENV PYTHONUNBUFFERED=1

EXPOSE 8501

CMD ["streamlit", "run", "streamlit.py", "--server.address", "0.0.0.0", "--server.port", "8501"]
