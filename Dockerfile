FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y \
    git \
    openssh-client \
    curl \
    unzip \
    && curl -fsSL https://releases.hashicorp.com/terraform/1.8.3/terraform_1.8.3_linux_amd64.zip -o terraform.zip \
    && unzip terraform.zip -d /usr/local/bin \
    && rm terraform.zip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
ENV GOOGLE_APPLICATION_CREDENTIALS=/app/credentials.json
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]