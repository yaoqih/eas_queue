# eas_queue

## Flask Queue Service with Celery

This service acts as a queue frontend for another API, similar to the behavior of PAI-EAS queue services.
It accepts requests, queues them using Celery with Redis, and forwards them asynchronously to a target API.

### Features

-   Receives POST requests at `/api/predict/<service_name>`.
-   Requires `Authorization` header.
-   Supports priority requests via `?_priority_=1` query parameter.
-   Returns `X-Eas-Queueservice-Request-Id` header and the Request ID as the body.
-   Uses Celery for asynchronous task processing.
-   Uses Redis as the Celery broker and result backend.
-   Includes basic retry logic for forwarding requests.

### Setup

1.  **Prerequisites:**
    *   Python 3.7+
    *   Redis server running

2.  **Clone the repository (if applicable):**
    ```bash
    # git clone ...
    cd eas_queue
    ```

3.  **Create and activate a virtual environment (recommended):**
    ```bash
    python -m venv venv
    # On Windows
    .\venv\Scripts\activate
    # On macOS/Linux
    source venv/bin/activate
    ```

4.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

5.  **Configure the service:**
    *   Edit `config.py`:
        *   Set `REDIS_URL` to your Redis instance URL (e.g., `redis://localhost:6379/0`).
        *   Set `TARGET_API_BASE_URL` to the base URL of the API you want to forward requests to (e.g., `http://localhost:5001`).
        *   Change `SECRET_KEY` to a unique, random string.

### Running the Service

You need to run two components: the Flask web server and the Celery worker(s).

1.  **Run the Celery Worker:**
    Open a terminal, activate the virtual environment, and run:
    ```bash
    celery -A app.celery_app worker --loglevel=info -Q celery,high_priority
    ```
    *   `-A app.celery_app`: Points to the Celery application instance in `app.py`.
    *   `--loglevel=info`: Sets the logging level.
    *   `-Q celery,high_priority`: Tells the worker to consume tasks from both the default (`celery`) and the `high_priority` queues.

2.  **Run the Flask Application:**
    Open another terminal, activate the virtual environment, and run:
    ```bash
    flask run --host=0.0.0.0 --port=5000
    ```
    *   This uses the Flask development server. For production, use a WSGI server like Gunicorn:
        ```bash
        # Example using Gunicorn
        # pip install gunicorn
        gunicorn -w 4 -b 0.0.0.0:5000 app:app
        ```

### Testing

Replace `<your_token>`, `<service_name>`, and the URL/port if different.

**Send a normal priority request:**

```bash
curl -v -X POST http://localhost:5000/api/predict/<service_name> \
     -H 'Authorization: Bearer <your_token>' \
     -H 'Content-Type: application/json' \
     -d '{"key": "value", "input": [1, 2, 3]}'
```

**Send a high priority request:**

```bash
curl -v -X POST 'http://localhost:5000/api/predict/<service_name>?_priority_=1' \
     -H 'Authorization: Bearer <your_token>' \
     -H 'Content-Type: application/json' \
     -d '{"key": "priority_value", "input": [4, 5, 6]}'
```

Check the Celery worker logs to see tasks being received and processed. Check the target API service to confirm it received the forwarded requests.

**Query Task Status (Sink Endpoint):**

After submitting a request, you will receive a `Request ID` in the response body and the `X-Eas-Queueservice-Request-Id` header. Use this ID to query the task's status via the sink endpoint, providing the original service name and the ID as a query parameter:

```bash
curl 'http://localhost:5000/api/predict/<service_name>/sink?request_id=<the_request_id_you_received>'
```

Replace `<service_name>` with the actual service name you used in the POST request and `<the_request_id_you_received>` with the ID you got back.

The response will be a JSON object indicating the task's status (`PENDING`, `STARTED`, `SUCCESS`, `FAILURE`, `RETRY`) and potentially the result or error information if the task has finished.

Example successful response:
```json
{
  "request_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "SUCCESS",
  "service_name": "my_image_service",
  "message": "Task completed successfully.",
  "result": {
    "status": "success",
    "target_status_code": 200
  }
}
```

Example failed response:
```json
{
  "request_id": "f0e9d8c7-b6a5-4321-fedc-ba9876543210",
  "status": "FAILURE",
  "service_name": "my_text_service",
  "message": "Task failed.",
  "error_info": "...error details from the task execution..."
}
```

Example pending/processing response (HTTP status code 202):
```json
{
  "request_id": "12345678-abcd-efab-cdef-0123456789ab",
  "status": "PENDING",
  "service_name": "my_service",
  "message": "Task is pending or request ID is invalid."
}
```

### Notes

*   **Target API:** You need a separate API running at `TARGET_API_BASE_URL` that can handle the forwarded POST requests.
*   **Error Handling:** The current error handling is basic. You might want to add more specific error handling, dead-letter queues, or monitoring.
*   **Authentication:** The `Authorization` header check is rudimentary. Implement proper token validation based on your needs.
*   **Scalability:** You can scale the processing power by running more Celery workers.
*   **Result Persistence:** Celery results are stored in the backend (Redis). Configure Redis persistence appropriately if you need results to survive restarts. Result expiration can also be configured in Celery.
