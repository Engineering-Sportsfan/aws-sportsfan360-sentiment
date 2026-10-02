Write-Host "🔑 Logging into AWS ECR..." -ForegroundColor Cyan
(aws ecr get-login-password --region us-east-1) | docker login --username AWS --password-stdin 536346448903.dkr.ecr.us-east-1.amazonaws.com

Write-Host "🔨 Building Docker Image (amd64 architecture for AWS Lambda)..." -ForegroundColor Cyan
docker buildx build --provenance=false --platform linux/amd64 -t aws-sportsfan360-sentiment .

Write-Host "🏷️ Tagging Image..." -ForegroundColor Cyan
docker tag aws-sportsfan360-sentiment:latest 536346448903.dkr.ecr.us-east-1.amazonaws.com/aws-sportsfan360-sentiment:latest

Write-Host "🚀 Pushing Image to AWS ECR..." -ForegroundColor Cyan
docker push 536346448903.dkr.ecr.us-east-1.amazonaws.com/aws-sportsfan360-sentiment:latest

Write-Host "🔄 Updating AWS Lambda Function Code..." -ForegroundColor Cyan
aws lambda update-function-code `
  --function-name aws-sportsfan360-sentiment `
  --image-uri 536346448903.dkr.ecr.us-east-1.amazonaws.com/aws-sportsfan360-sentiment:latest `
  --region us-east-1

Write-Host "✅ Deployment Complete! The bots are live on AWS." -ForegroundColor Green
