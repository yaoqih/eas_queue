import uuid
import logging
from flask import Flask, request, jsonify, make_response
from celery import Celery, Task
from celery.exceptions import MaxRetriesExceededError
import requests
import json
from config import REDIS_URL, TARGET_API_BASE_URL, SECRET_KEY

# --- Flask App Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY

# --- Celery Configuration ---
# Make sure the broker and backend URLs point to your Redis instance
# Celery task priorities range from 0 (highest) to 9 (lowest). Default is usually 4 or 5.
# We map our simple priority (1=high, 0=normal) to Celery priorities.
NORMAL_PRIORITY = 5
HIGH_PRIORITY = 2

celery_app = Celery('eas_queue_tasks',
                  broker=REDIS_URL,
                  backend=REDIS_URL)

celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    # Configure queues for priority
    task_queues={
        'celery': { # Default queue
            'exchange': 'celery',
            'routing_key': 'celery',
            'priority': NORMAL_PRIORITY, # Default priority for this queue
        },
         'high_priority': {
            'exchange': 'high_priority',
            'routing_key': 'high_priority',
            'priority': HIGH_PRIORITY, # Higher priority queue
        }
    },
    task_default_queue='celery',
    task_default_exchange='celery',
    task_default_routing_key='celery',
)


# Optional: Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Celery Task Definition ---
class ForwardRequestTask(Task):
    autoretry_for = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
    retry_kwargs = {'max_retries': 3}
    retry_backoff = True
    retry_backoff_max = 700 # seconds
    retry_jitter = False # avoid adding random delays

    def run(self, request_id, service_name, original_headers, data, target_url):
        """
        Task to forward the request to the target API.
        """
        logger.info(f"[{request_id}] Processing request for service: {service_name}")

        headers_to_forward = {k: v for k, v in original_headers.items() if k.lower() not in ['host', 'authorization', 'content-length']}
        # Add the request ID to the forwarded headers for traceability
        headers_to_forward['X-Original-Request-Id'] = request_id
        headers_to_forward['Content-Type'] = 'application/json' # Assume JSON for forwarding

        try:
            logger.info(f"[{request_id}] Forwarding request to: {target_url}")
            logger.debug(f"[{request_id}] Forwarding headers: {headers_to_forward}")
            logger.debug(f"[{request_id}] Forwarding data: {data}")

            response = requests.post(target_url, headers=headers_to_forward, json=data, timeout=30) # Adjust timeout as needed

            response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)

            logger.info(f"[{request_id}] Successfully forwarded request to {target_url}. Status: {response.status_code}")
            # Optionally, store or log the result from the target API
            # result_data = response.json()
            # logger.debug(f"[{request_id}] Target API response: {result_data}")
            return {'status': 'success', 'target_status_code': response.status_code}

        except requests.exceptions.RequestException as e:
            logger.error(f"[{request_id}] Failed to forward request to {target_url}: {e}")
            # Celery's autoretry will handle retries based on autoretry_for exceptions
            # If max retries are exceeded, Celery raises MaxRetriesExceededError
            # We explicitly raise the exception again to ensure Celery handles it
            raise self.retry(exc=e)
        except Exception as e:
            # Catch unexpected errors during processing
            logger.error(f"[{request_id}] An unexpected error occurred: {e}", exc_info=True)
            # Decide if this unexpected error should be retried or marked as failed
            # For now, we don't retry unexpected errors.
            return {'status': 'failed', 'error': str(e)}

# Register the task with Celery
forward_request = celery_app.register_task(ForwardRequestTask())


# --- Query Task Status Endpoint (Sink Style) ---
# Matches the PAI-EAS sink pattern
@app.route('/api/predict/<service_name>/sink', methods=['GET'])
def get_task_status(service_name):
    """
    Queries the status and result of a task using its Request ID passed as a query parameter.
    Example: GET /api/predict/my_service/sink?request_id=xxx-xxx-xxx
    """
    request_id = request.args.get('request_id')
    if not request_id:
        logger.warning(f"Missing 'request_id' query parameter for sink lookup on service: {service_name}")
        return jsonify({"error": "'request_id' query parameter is required"}), 400

    logger.info(f"Querying sink for service '{service_name}', request ID: {request_id}")
    try:
        # Fetch the result object from the Celery backend using the request_id
        task_result = celery_app.AsyncResult(request_id)

        response_data = {
            'request_id': request_id,
            'status': task_result.state,
            'service_name': service_name # Include service name for context
        }

        if task_result.state == 'PENDING':
            # Task is waiting to be executed or unknown ID
            response_data['message'] = 'Task is pending or request ID is invalid.'
            return jsonify(response_data), 202 # Accepted, but not processed yet
        elif task_result.state == 'STARTED':
            response_data['message'] = 'Task has started processing.'
            return jsonify(response_data), 202
        elif task_result.state == 'RETRY':
            response_data['message'] = 'Task is being retried.'
            response_data['error_info'] = str(task_result.info) # Get retry exception info
            return jsonify(response_data), 202
        elif task_result.state == 'SUCCESS':
            response_data['message'] = 'Task completed successfully.'
            response_data['result'] = task_result.get() # Get the return value of the task
            return jsonify(response_data), 200
        elif task_result.state == 'FAILURE':
            response_data['message'] = 'Task failed.'
            response_data['error_info'] = str(task_result.info)
            # response_data['traceback'] = task_result.traceback # Optionally include traceback
            return jsonify(response_data), 500 # Internal Server Error status for task failure
        else:
             response_data['message'] = f'Unknown task state: {task_result.state}'
             return jsonify(response_data), 500

    except Exception as e:
        logger.error(f"Error querying sink for service '{service_name}', request ID {request_id}: {e}", exc_info=True)
        return jsonify({"error": "Failed to query task status", "details": str(e)}), 500


# --- Flask Routes ---
@app.route('/api/predict/<service_name>', methods=['POST'])
def enqueue_request(service_name):
    """
    Receives requests, validates them, and puts them onto the Celery queue.
    """
    request_id = str(uuid.uuid4())
    logger.info(f"[{request_id}] Received request for service: {service_name}")

    # 1. Check Authorization Header
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        logger.warning(f"[{request_id}] Missing Authorization header.")
        return jsonify({"error": "Authorization header is required"}), 401
    # Add more sophisticated token validation here if needed

    # 2. Get Request Data
    try:
        # Use get_data() to handle non-JSON or empty bodies gracefully
        raw_data = request.get_data()
        if not raw_data:
             logger.warning(f"[{request_id}] Received empty request body.")
             # Decide if empty body is allowed, here we assume it needs some data
             # If you need to handle specific content types like form data, adjust parsing
             return jsonify({"error": "Request body cannot be empty"}), 400

        # Attempt to parse as JSON, but forward raw data if not JSON
        try:
             data = json.loads(raw_data)
        except json.JSONDecodeError:
             logger.warning(f"[{request_id}] Request body is not valid JSON. Forwarding raw data.")
             # If target expects JSON, this might fail later. Consider validation.
             data = raw_data.decode('utf-8') # Forward as string if not JSON

    except Exception as e:
        logger.error(f"[{request_id}] Error reading request data: {e}")
        return jsonify({"error": "Failed to process request data"}), 400

    # 3. Determine Priority
    is_priority = request.args.get('_priority_') == '1'
    task_priority = HIGH_PRIORITY if is_priority else NORMAL_PRIORITY
    queue_name = 'high_priority' if is_priority else 'celery' # Route to different queues
    logger.info(f"[{request_id}] Priority: {'High' if is_priority else 'Normal'} (Celery Priority: {task_priority}, Queue: {queue_name})")


    # 4. Construct Target URL (Optional - could be done in the task)
    # Assuming the target service uses the same service_name path
    target_url = f"{TARGET_API_BASE_URL}/api/predict/{service_name}"
    # Alternatively, use a different mapping if needed:
    # target_url = f"{TARGET_API_BASE_URL}/some/other/path/{service_name}"

    # 5. Enqueue the Task
    try:
        # Pass necessary info to the task
        # Convert Headers object to a simple dict for serialization
        headers_dict = dict(request.headers)

        forward_request.apply_async(
            args=[request_id, service_name, headers_dict, data, target_url],
            priority=task_priority,
            queue=queue_name,
            # Add other options like countdown, eta if needed
        )
        logger.info(f"[{request_id}] Task enqueued successfully to queue '{queue_name}'.")

    except Exception as e:
        logger.error(f"[{request_id}] Failed to enqueue task: {e}", exc_info=True)
        return jsonify({"error": "Failed to enqueue request"}), 500

    # 6. Prepare and Send Response
    # Use the generated request_id as the index/identifier
    response_body = request_id
    resp = make_response(response_body, 200)
    resp.headers['X-Eas-Queueservice-Request-Id'] = request_id
    resp.headers['Content-Type'] = 'text/plain' # Or application/json if you prefer {"index": request_id}

    logger.info(f"[{request_id}] Responding with Request ID.")
    return resp

# --- Health Check Endpoint (Optional) ---
@app.route('/health', methods=['GET'])
def health_check():
    # Basic health check
    return jsonify({"status": "ok"}), 200

# --- Main Execution ---
if __name__ == '__main__':
    # Note: Use a proper WSGI server like Gunicorn or uWSGI in production
    app.run(host='0.0.0.0', port=5000, debug=True) # Set debug=False for production 