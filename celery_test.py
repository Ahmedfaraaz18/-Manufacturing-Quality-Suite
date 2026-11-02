from celery import Celery

app = Celery(
    "readiness_test",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1"
)

@app.task
def add_numbers(a, b):
    return a + b