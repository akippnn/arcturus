FROM docker.io/python:3.11-alpine

ARG TERRAFORM_VERSION=1.10.5

RUN apk add --no-cache curl gnupg docker-cli docker-compose git bash && \
    cd /tmp && \
    curl -4 -fsSLO "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" && \
    curl -4 -fsSLO "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_SHA256SUMS" && \
    grep "terraform_${TERRAFORM_VERSION}_linux_amd64.zip" "terraform_${TERRAFORM_VERSION}_SHA256SUMS" | sha256sum -c - && \
    unzip "terraform_${TERRAFORM_VERSION}_linux_amd64.zip" -d /usr/local/bin/ && \
    rm "terraform_${TERRAFORM_VERSION}_linux_amd64.zip" "terraform_${TERRAFORM_VERSION}_SHA256SUMS" && \
    chmod +x /usr/local/bin/terraform

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
