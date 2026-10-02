import os
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

# Initialize boto3 DynamoDB resource
region = os.getenv("AWS_REGION", "us-east-1")
dynamodb = boto3.resource("dynamodb", region_name=region)

STAGE = os.getenv("STAGE", os.getenv("ENV", "dev")).lower()

def get_table_name(base_name: str) -> str:
    # Explicit override takes priority if set in .env
    override = os.getenv(f"DYNAMODB_{base_name.upper()}_TABLE")
    if override:
        return override
    
    if STAGE in ["prod", "production"]:
        return base_name
    return f"{base_name}-{STAGE}"

def get_table(name: str):
    real_name = get_table_name(name)
    return dynamodb.Table(real_name)
