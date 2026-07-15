FROM public.ecr.aws/lambda/python:3.11

COPY requirements.txt ${LAMBDA_TASK_ROOT}
RUN dnf install -y gcc rust cargo && pip install --upgrade pip && pip install -r requirements.txt

COPY . ${LAMBDA_TASK_ROOT}

CMD [ "main.handler" ]
