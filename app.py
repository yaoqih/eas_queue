import uuid
import logging
from flask import Flask, request, jsonify, make_response
from celery import Celery, Task
from celery.exceptions import MaxRetriesExceededError
import requests
import json
from config import REDIS_URL, TARGET_API_BASE_URL, SECRET_KEY
import redis
import time # Import time for TTL

# --- Flask App Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY

# --- Redis Client Setup ---
# Initialize redis_client to None first
redis_client = None
# Define a key prefix for index mapping
INDEX_PREFIX = "easq:index:"
REQID_PREFIX = "easq:reqid:" # Optional: for reverse lookup if needed
INDEX_COUNTER_KEY = "easq:request_index_counter"
DEFAULT_TTL = 86400 # 24 hours in seconds

try:
    # Note: Using app.logger requires the app context or setup after app creation.
    # We might log directly using logging here if app context is not yet fully available.
    logging.info(f"Attempting to connect to Redis at {REDIS_URL}")
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=False) # Use decode_responses=False for raw bytes
    redis_client.ping() # Check connection
    logging.info("Successfully connected to Redis for index management.")
except redis.exceptions.ConnectionError as e:
    logging.error(f"Failed to connect to Redis at {REDIS_URL}: {e}")
    # redis_client remains None if connection fails
except Exception as e:
    # Catch other potential errors during Redis init
    logging.error(f"An unexpected error occurred during Redis initialization: {e}")
    redis_client = None # Ensure it's None on other errors too


# --- Celery Configuration ---
# Make sure the broker and backend URLs point to your Redis instance
# Celery task priorities range from 0 (highest) to 9 (lowest). Default is usually 4 or 5.
# We map our simple priority (1=high, 0=normal) to Celery priorities.
NORMAL_PRIORITY = 5
HIGH_PRIORITY = 2

celery_app = Celery('lavieai_task_queue_tasks',
                  broker=REDIS_URL,
                  backend=REDIS_URL)

celery_app.conf.update(
    task_serializer='pickle', # Use pickle to serialize bytes content
    accept_content=['pickle', 'json'], # Accept pickle for results
    result_serializer='pickle', # Serialize results as pickle
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
    # Set result_expires to clean up results automatically (e.g., 24 hours)
    result_expires=DEFAULT_TTL,
)


# Optional: Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Celery Task Definition ---
@celery_app.task(bind=True,
                 name='lavieai_task_queue_tasks.forward_request',
                 autoretry_for=(requests.exceptions.ConnectionError, requests.exceptions.Timeout),
                 retry_kwargs={'max_retries': 3},
                 retry_backoff=True,
                 retry_backoff_max=700, # seconds
                 retry_jitter=False,
                 result_extended=True) # Store extended result properties like content type
def forward_request(self, request_id, service_name, subpath, original_headers, data):
    """
    Task to forward the request to the target API using function-based syntax with decorator.
    Handles constructing the final target URL with subpath.
    Returns a dictionary containing the raw response content and content type on success.
    """
    logger.info(f"[{request_id}] Processing request for service: {service_name}, subpath: {subpath}")

    # Construct the target URL inside the task using only the subpath
    # Ensure TARGET_API_BASE_URL and subpath are joined correctly
    base_url = TARGET_API_BASE_URL.rstrip('/')
    path_segment = subpath.lstrip('/')

    if path_segment:
        target_url = f"{base_url}/{path_segment}"
    else:
        # If subpath is empty, target the base URL itself
        target_url = base_url

    logger.info(f"[{request_id}] Constructed target URL: {target_url}")


    headers_to_forward = {k: v for k, v in original_headers.items() if k.lower() not in ['host', 'authorization', 'content-length', 'content-type']} # Remove Content-Type too
    # Add the request ID to the forwarded headers for traceability
    headers_to_forward['X-Original-Request-Id'] = request_id
    # Set Content-Type based on the original request if possible, or default
    # Note: 'data' might be string (if original was not JSON) or dict/list (if JSON)
    content_type_to_send = original_headers.get('Content-Type', 'application/json') # Default to JSON if original missing
    headers_to_forward['Content-Type'] = content_type_to_send


    try:
        logger.info(f"[{request_id}] Forwarding request to: {target_url}")
        logger.debug(f"[{request_id}] Forwarding headers: {headers_to_forward}")
        logger.debug(f"[{request_id}] Forwarding data type: {type(data)}") # Log data type
        # print(f"[{request_id}] Forwarding data: {data}") # Careful logging potentially large data

        # Send data appropriately based on Content-Type
        kwargs = {'timeout': 60} # Increased timeout
        if 'application/json' in content_type_to_send and isinstance(data, (dict, list)):
            kwargs['json'] = data
        elif isinstance(data, str):
            kwargs['data'] = data.encode('utf-8') # Send string as bytes
        elif isinstance(data, bytes):
             kwargs['data'] = data # Send raw bytes
        else:
             # Fallback: try sending as JSON string
             kwargs['data'] = json.dumps(data).encode('utf-8')
             headers_to_forward['Content-Type'] = 'application/json' # Ensure header matches

        response = requests.post(target_url, headers=headers_to_forward, **kwargs)

        response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)

        # Get raw content and content type
        content = response.content # Get raw bytes
        content_type = response.headers.get('Content-Type', 'application/octet-stream') # Get content type or default
        logger.info(f"[{request_id}] Successfully forwarded request to {target_url}. Status: {response.status_code}, Content-Type: {content_type}, Content Length: {len(content)}")

        # Return only the relevant API result parts
        return {'status': 'success', 'content': content, 'content_type': content_type}

    except requests.exceptions.RequestException as e:
        logger.error(f"[{request_id}] Failed to forward request to {target_url}: {e}")
        # Celery's autoretry handles retries based on autoretry_for exceptions
        try:
            # Use self.retry because bind=True
            # Pass the original exception for accurate retry/failure info
            self.retry(exc=e)
        except MaxRetriesExceededError as max_retry_e:
             logger.error(f"[{request_id}] Max retries exceeded for request to {target_url}: {max_retry_e}")
             # Return failure status explicitly if max retries are hit
             return {'status': 'failed', 'error': f'Max retries exceeded: {str(e)}'}
        except Exception as retry_e: # Catch potential errors during retry call itself
             logger.error(f"[{request_id}] Error invoking retry for request to {target_url}: {retry_e}")
             # Return failure if retry mechanism fails
             return {'status': 'failed', 'error': f'Retry mechanism failed: {str(retry_e)}'}

    except Exception as e:
        # Catch unexpected errors during processing
        logger.error(f"[{request_id}] An unexpected error occurred: {e}", exc_info=True)
        # Don't retry unexpected errors. Mark as failed.
        return {'status': 'failed', 'error': f'Unexpected task error: {str(e)}'}


# --- Query Task Status Endpoint (Sink Style) ---
# Matches the PAI-EAS sink pattern
@app.route('/api/predict/<service_name>/sink', methods=['GET'])
def get_task_status(service_name):
    """
    Queries the status and result of a task using its Request ID (request_id) or Index (_index_)
    passed as query parameters. Returns the raw API response content on success.
    Example:
    GET /api/predict/my_service/sink?request_id=xxx-xxx-xxx
    GET /api/predict/my_service/sink?_index_=123
    """
    query_request_id = request.args.get('request_id')
    query_index = request.args.get('_index_')
    request_id_to_query = None
    query_identifier_log = ""

    if not redis_client:
         logger.error("Redis client is not available for sink query.")
         return jsonify({"error": "Internal server error: Cannot connect to backend storage"}), 503 # Service Unavailable

    if query_index:
        query_identifier_log = f"index: {query_index}"
        try:
            # Ensure index is treated as string for key generation
            redis_key = f"{INDEX_PREFIX}{query_index}"
            # Use get() which returns bytes because decode_responses=False
            request_id_bytes = redis_client.get(redis_key)
            if request_id_bytes:
                request_id_to_query = request_id_bytes.decode('utf-8') # Decode bytes to string
                logger.info(f"Looked up request ID '{request_id_to_query}' using index '{query_index}'")
            else:
                logger.warning(f"Index '{query_index}' not found in Redis for sink lookup on service: {service_name}")
                return jsonify({"error": f"Index '{query_index}' not found or expired"}), 404
        except redis.exceptions.RedisError as e:
             logger.error(f"Redis error looking up index '{query_index}': {e}")
             return jsonify({"error": "Failed to query backend storage for index"}), 500
        except Exception as e:
             logger.error(f"Unexpected error looking up index '{query_index}': {e}")
             return jsonify({"error": "Internal server error during index lookup"}), 500

    elif query_request_id:
        query_identifier_log = f"request_id: {query_request_id}"
        request_id_to_query = query_request_id
    else:
        logger.warning(f"Missing 'request_id' or '_index_' query parameter for sink lookup on service: {service_name}")
        return jsonify({"error": "Either 'request_id' or '_index_' query parameter is required"}), 400

    logger.info(f"Querying sink for service '{service_name}', using {query_identifier_log}")

    try:
        # Fetch the result object from the Celery backend using the request_id_to_query
        task_result = celery_app.AsyncResult(request_id_to_query)

        # Prepare a base JSON response for non-success/non-204 cases
        response_data = {
            'query_identifier': query_index or query_request_id, # Indicate what was used for query
            'request_id': request_id_to_query, # The actual Celery Task ID
            'status': task_result.state,
            'service_name': service_name
        }

        if task_result.state == 'PENDING':
            logger.info(f"[{request_id_to_query}] Task is PENDING. Responding with 204.")
            return make_response('', 204) # Return 204 No Content for pending
        elif task_result.state == 'STARTED':
            logger.info(f"[{request_id_to_query}] Task is STARTED. Responding with 204.")
            return make_response('', 204) # Return 204 No Content for started
        elif task_result.state == 'RETRY':
            logger.info(f"[{request_id_to_query}] Task is being RETRIED. Responding with 204.")
            return make_response('', 204) # Return 204 No Content for retry
        elif task_result.state == 'SUCCESS':
            task_output = task_result.get() # Get the return value dict
            # Check if the output is the expected dictionary format from our task
            if isinstance(task_output, dict) and 'status' in task_output:
                if task_output.get('status') == 'success':
                    content = task_output.get('content') # Should be bytes
                    content_type = task_output.get('content_type', 'application/octet-stream')

                    if content is None or content == b'': # Check for empty bytes
                        logger.info(f"[{request_id_to_query}] Task succeeded but target API returned no content. Responding with 204.")
                        return make_response('', 204) # Return 204 No Content
                    else:
                        logger.info(f"[{request_id_to_query}] Task succeeded. Returning target API content ({len(content)} bytes) with Content-Type: {content_type}")
                        resp = make_response(content, 200)
                        resp.headers['Content-Type'] = content_type
                        # Optionally add back the request ID header for confirmation
                        resp.headers['X-Eas-Queueservice-Request-Id'] = request_id_to_query
                        return resp
                else: # Task finished but reported 'failed' status internally
                     logger.error(f"[{request_id_to_query}] Task finished with internal failure status: {task_output.get('error', 'Unknown internal error')}")
                     response_data['message'] = 'Task processing reported an internal failure.'
                     response_data['error_info'] = task_output.get('error', 'No error details provided.')
                     response_data['status'] = 'FAILURE' # Treat internal failure as overall failure
                     return jsonify(response_data), 500
            else:
                # Handle cases where task succeeded but returned unexpected format
                logger.error(f"[{request_id_to_query}] Task succeeded but returned unexpected result format: {type(task_output)}")
                response_data['message'] = 'Task completed successfully but result format is unexpected.'
                try:
                    # Try to give a preview, careful with large/binary data
                    response_data['result_preview'] = repr(task_output)[:500]
                except Exception:
                    response_data['result_preview'] = "[Could not represent result preview]"
                return jsonify(response_data), 200 # Still 200 as Celery reported SUCCESS

        elif task_result.state == 'FAILURE':
            response_data['message'] = 'Task failed processing.'
            # task_result.info usually contains the exception object on failure
            if task_result.info and isinstance(task_result.info, Exception):
                 response_data['error_info'] = str(task_result.info)
            elif task_result.info:
                 response_data['error_info'] = repr(task_result.info) # Fallback representation

            # response_data['traceback'] = task_result.traceback # Optionally include traceback
            logger.error(f"[{request_id_to_query}] Task failed. Info: {response_data.get('error_info', 'N/A')}")
            return jsonify(response_data), 500 # Internal Server Error status for task failure
        else:
             # Handle any other unknown Celery states
             response_data['message'] = f'Unknown task state: {task_result.state}'
             logger.warning(f"[{request_id_to_query}] Encountered unknown task state: {task_result.state}")
             return jsonify(response_data), 500

    except Exception as e:
        logger.error(f"Error querying sink for service '{service_name}', identifier {query_identifier_log}: {e}", exc_info=True)
        return jsonify({"error": "Failed to query task status", "details": str(e)}), 500


# --- Flask Routes ---
@app.route('/api/predict/<path:full_path>', methods=['POST'])
def enqueue_request(full_path):
    """
    Receives requests, validates them, handles subpaths, generates an index and request_id,
    stores the index->request_id mapping in Redis, and puts the task onto the Celery queue.
    Returns the index as plain text in the response body and request_id in the header.
    Example: '/api/predict/service_a/sub/path' -> full_path = 'service_a/sub/path'
    """
    request_id = str(uuid.uuid4()) # Generate unique request ID
    index = None # Initialize index

    # Check for Redis connection early
    if not redis_client:
        logger.error(f"[{request_id}] Cannot enqueue task: Redis client is not available.")
        return jsonify({"error": "Internal server error: Backend storage unavailable"}), 503


    path_parts = full_path.split('/', 1)
    service_name = path_parts[0]
    subpath = path_parts[1] if len(path_parts) > 1 else ""

    logger.info(f"[{request_id}] Received request for service: {service_name}, subpath: '/{subpath}'")


    # 1. Check Authorization Header
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        logger.warning(f"[{request_id}] Missing Authorization header.")
        return jsonify({"error": "Authorization header is required"}), 401
    # Add more sophisticated token validation here if needed

    # 2. Get Request Data (handle different content types)
    try:
        raw_data = request.get_data() # Get raw bytes first
        content_type = request.content_type

        if not raw_data:
             logger.warning(f"[{request_id}] Received empty request body.")
             # Allow empty body, forward None or empty bytes
             data_to_forward = raw_data # Forward empty bytes
        elif content_type and 'application/json' in content_type.lower():
            try:
                # Parse JSON if content type indicates it
                data_to_forward = json.loads(raw_data.decode('utf-8'))
                logger.debug(f"[{request_id}] Parsed JSON request body.")
            except json.JSONDecodeError:
                logger.warning(f"[{request_id}] Content-Type is JSON, but body is not valid JSON. Forwarding raw bytes.")
                data_to_forward = raw_data # Forward raw bytes if JSON parsing fails
        else:
            # For other content types (or no content type), forward raw bytes
            logger.info(f"[{request_id}] Forwarding raw request body (Content-Type: {content_type or 'N/A'}).")
            data_to_forward = raw_data

    except Exception as e:
        logger.error(f"[{request_id}] Error reading request data: {e}")
        return jsonify({"error": "Failed to process request data"}), 400

    # 3. Generate Index and Store Mapping in Redis
    try:
        # Increment counter to get the next index ID
        index = redis_client.incr(INDEX_COUNTER_KEY)
        index_key = f"{INDEX_PREFIX}{index}"
        # Store mapping from index (string key) to request_id (bytes value) with TTL
        # Ensure request_id is stored as bytes because decode_responses=False
        redis_client.setex(index_key, DEFAULT_TTL, request_id.encode('utf-8'))
        logger.info(f"[{request_id}] Generated index {index} and stored mapping in Redis with TTL {DEFAULT_TTL}s.")
    except redis.exceptions.RedisError as e:
        logger.error(f"[{request_id}] Redis error generating index or storing mapping: {e}")
        # If index generation fails, we cannot proceed reliably
        return jsonify({"error": "Failed to generate request index due to backend storage error"}), 500
    except Exception as e:
         logger.error(f"[{request_id}] Unexpected error during index generation: {e}")
         return jsonify({"error": "Internal server error during index generation"}), 500

    # 4. Determine Priority
    is_priority = request.args.get('_priority_') == '1'
    task_priority = HIGH_PRIORITY if is_priority else NORMAL_PRIORITY
    queue_name = 'high_priority' if is_priority else 'celery' # Route to different queues
    logger.info(f"[{request_id}] Priority: {'High' if is_priority else 'Normal'} (Celery Priority: {task_priority}, Queue: {queue_name})")


    # 5. Enqueue the Task
    try:
        # Convert Headers object to a simple dict for serialization
        headers_dict = dict(request.headers)

        forward_request.apply_async(
            args=[request_id, service_name, subpath, headers_dict, data_to_forward], # Pass parsed/raw data
            priority=task_priority,
            queue=queue_name,
            task_id=request_id, # Use generated uuid as Celery task_id for lookup
            serializer='pickle' # Explicitly use pickle for this task if needed
            # Add other options like countdown, eta if needed
        )
        logger.info(f"[{request_id}] Task enqueued successfully to queue '{queue_name}' with task_id={request_id} and index={index}.")

    except Exception as e:
        logger.error(f"[{request_id}] Failed to enqueue task: {e}", exc_info=True)
        # Attempt to clean up the index mapping if enqueue fails? Maybe not, user might retry.
        return jsonify({"error": "Failed to enqueue request"}), 500

    # 6. Prepare and Send Response (Return Index in body)
    response_body = str(index) # Return the numeric index as plain text
    resp = make_response(response_body, 200) # OK status
    resp.headers['X-Eas-Queueservice-Request-Id'] = request_id # Keep header for compatibility/lookup
    resp.headers['Content-Type'] = 'text/plain; charset=utf-8' # Specify plain text

    logger.info(f"[{request_id}] Responding with Index: {index}")
    return resp

# --- Health Check Endpoint (Optional) ---
@app.route('/health', methods=['GET'])
def health_check():
    # Basic health check, could add Redis/Celery ping here
    health_status = {"status": "ok", "redis_connected": bool(redis_client)}
    status_code = 200 if redis_client else 503
    try:
        if redis_client:
             redis_client.ping()
    except redis.exceptions.ConnectionError:
         health_status["redis_connected"] = False
         health_status["status"] = "degraded"
         status_code = 503
    except Exception as e:
         health_status["redis_error"] = str(e)
         health_status["status"] = "error"
         status_code = 500

    return jsonify(health_status), status_code


# --- Main Execution ---
if __name__ == '__main__':
    # Note: Use a proper WSGI server like Gunicorn or uWSGI in production
    # Set debug=False for production
    app.run(host='0.0.0.0', port=5002, debug=False) 