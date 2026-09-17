import os
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

# Initialize boto3 DynamoDB resource
region = os.getenv("AWS_REGION", "us-east-1")
dynamodb = boto3.resource("dynamodb", region_name=region)

def get_table(name: str):
    return dynamodb.Table(name)
