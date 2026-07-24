#!/bin/bash
set -e

echo "🔑 Logging into AWS ECR..."
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 536346448903.dkr.ecr.us-east-1.amazonaws.com

echo "🔨 Building Docker Image (amd64 architecture for AWS Lambda)..."
docker build --platform linux/amd64 -t aws-sportsfan360-sentiment .

echo "🏷️ Tagging Image..."
docker tag aws-sportsfan360-sentiment:latest 536346448903.dkr.ecr.us-east-1.amazonaws.com/aws-sportsfan360-sentiment:latest

echo "🚀 Pushing Image to AWS ECR..."
docker push 536346448903.dkr.ecr.us-east-1.amazonaws.com/aws-sportsfan360-sentiment:latest

echo "🔄 Updating AWS Lambda Function Code..."
aws lambda update-function-code \
  --function-name sportsfan-sentiment-engine \
  --image-uri 536346448903.dkr.ecr.us-east-1.amazonaws.com/aws-sportsfan360-sentiment:latest \
  --region us-east-1 > /dev/null

echo "✅ Deployment Complete! The bots are live on AWS."
