import copy
import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures.thread import ThreadPoolExecutor
from itertools import islice
from typing import Dict, Iterable, List, Literal, Optional, Tuple

from botocore.utils import InvalidArnException
from moto.sqs.models import BINARY_TYPE_FIELD_INDEX, STRING_TYPE_FIELD_INDEX
from moto.sqs.models import Message as MotoMessage
from werkzeug import Request as WerkzeugRequest

from localstack import config
from localstack.aws.api import CommonServiceException, RequestContext, ServiceException
from localstack.aws.api.sqs import (
    ActionNameList,
    AttributeNameList,
    AWSAccountIdList,
    BatchEntryIdsNotDistinct,
    BatchRequestTooLong,
    BatchResultErrorEntry,
    BoxedInteger,
    CancelMessageMoveTaskResult,
    ChangeMessageVisibilityBatchRequestEntryList,
    ChangeMessageVisibilityBatchResult,
    CreateQueueResult,
    DeleteMessageBatchRequestEntryList,
    DeleteMessageBatchResult,
    DeleteMessageBatchResultEntry,
    EmptyBatchRequest,
    GetQueueAttributesResult,
    GetQueueUrlResult,
    InvalidAttributeName,
    InvalidBatchEntryId,
    InvalidMessageContents,
    ListDeadLetterSourceQueuesResult,
    ListMessageMoveTasksResult,
    ListMessageMoveTasksResultEntry,
    ListQueuesResult,
    ListQueueTagsResult,
    Message,
    MessageAttributeNameList,
    MessageBodyAttributeMap,
    MessageBodySystemAttributeMap,
    MessageSystemAttributeList,
    MessageSystemAttributeName,
    NullableInteger,
    PurgeQueueInProgress,
    QueueAttributeMap,
    QueueAttributeName,
    QueueDeletedRecently,
    QueueDoesNotExist,
    QueueNameExists,
    ReceiveMessageResult,
    ResourceNotFoundException,
    SendMessageBatchRequestEntryList,
    SendMessageBatchResult,
    SendMessageBatchResultEntry,
    SendMessageResult,
    SqsApi,
    StartMessageMoveTaskResult,
    String,
    TagKeyList,
    TagMap,
    Token,
    TooManyEntriesInBatchRequest,
)
from localstack.aws.protocol.parser import create_parser
from localstack.aws.protocol.serializer import aws_response_serializer
from localstack.aws.spec import load_service
from localstack.config import SQS_DISABLE_MAX_NUMBER_OF_MESSAGE_LIMIT
from localstack.http import Request, route
from localstack.services.edge import ROUTER
from localstack.services.plugins import ServiceLifecycleHook
from localstack.services.sqs import constants as sqs_constants
from localstack.services.sqs import query_api
from localstack.services.sqs.constants import (
    HEADER_LOCALSTACK_SQS_OVERRIDE_MESSAGE_COUNT,
    HEADER_LOCALSTACK_SQS_OVERRIDE_WAIT_TIME_SECONDS,
    MAX_RESULT_LIMIT,
)
from localstack.services.sqs.exceptions import (
    InvalidParameterValueException,
    MissingRequiredParameterException,
)
from localstack.services.sqs.models import (
    FifoQueue,
    MessageMoveTask,
    MessageMoveTaskStatus,
    SqsMessage,
    SqsQueue,
    SqsStore,
    StandardQueue,
    sqs_stores,
)
from localstack.services.sqs.utils import (
    decode_move_task_handle,
    generate_message_id,
    is_fifo_queue,
    is_message_deduplication_id_required,
    parse_queue_url,
)
from localstack.services.stores import AccountRegionBundle
from localstack.utils.aws.arns import parse_arn
from localstack.utils.aws.request_context import extract_region_from_headers
from localstack.utils.bootstrap import is_api_enabled
from localstack.utils.cloudwatch.cloudwatch_util import (
    SqsMetricBatchData,
    publish_sqs_metric,
    publish_sqs_metric_batch,
)
from localstack.utils.collections import PaginatedList
from localstack.utils.run import FuncThread
from localstack.utils.scheduler import Scheduler
from localstack.utils.strings import md5, token_generator
from localstack.utils.threads import start_thread
from localstack.utils.time import now

LOG = logging.getLogger(__name__)

MAX_NUMBER_OF_MESSAGES = 10
_STORE_LOCK = threading.RLock()


class InvalidAddress(ServiceException):
    code = "InvalidAddress"
    message = "The address https://queue.amazonaws.com/ is not valid for this endpoint."
    sender_fault = True
    status_code = 404


def assert_queue_name(queue_name: str, fifo: bool = False):
    if queue_name.endswith(".fifo"):
        if not fifo:
            # Standard queues with .fifo suffix are not allowed
            raise InvalidParameterValueException(
                "Can only include alphanumeric characters, hyphens, or underscores. 1 to 80 in length"
            )
        # The .fifo suffix counts towards the 80-character queue name quota.
        queue_name = queue_name[:-5] + "_fifo"

    # slashes are actually not allowed, but we've allowed it explicitly in localstack
    if not re.match(r"^[a-zA-Z0-9/_-]{1,80}$", queue_name):
        raise InvalidParameterValueException(
            "Can only include alphanumeric characters, hyphens, or underscores. 1 to 80 in length"
        )


def check_message_min_size(message_body: str):
    if _message_body_size(message_body) == 0:
        raise MissingRequiredParameterException(
            "The request must contain the parameter MessageBody."
        )


def check_message_max_size(
    message_body: str, message_attributes: MessageBodyAttributeMap, max_message_size: int
):
    # https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/quotas-messages.html
    error = "One or more parameters are invalid. "
    error += f"Reason: Message must be shorter than {max_message_size} bytes."
    if (
        _message_body_size(message_body) + _message_attributes_size(message_attributes)
        > max_message_size
    ):
        raise InvalidParameterValueException(error)


def _message_body_size(body: str):
    return _bytesize(body)


def _message_attributes_size(attributes: MessageBodyAttributeMap):
    if not attributes:
        return 0
    message_attributes_keys_size = sum(_bytesize(k) for k in attributes.keys())
    message_attributes_values_size = sum(
        sum(_bytesize(v) for v in attr.values()) for attr in attributes.values()
    )
    return message_attributes_keys_size + message_attributes_values_size


def _bytesize(value: str | bytes):
    # must encode as utf8 to get correct bytes with len
    return len(value.encode("utf8")) if isinstance(value, str) else len(value)


def check_message_content(message_body: str):
    error = "Invalid characters found. Valid unicode characters are #x9 | #xA | #xD | #x20 to #xD7FF | #xE000 to #xFFFD | #x10000 to #x10FFFF"

    if not re.match(sqs_constants.MSG_CONTENT_REGEX, message_body):
        raise InvalidMessageContents(error)


class CloudwatchDispatcher:
    """
    Dispatches SQS metrics for specific api-calls using a ThreadPool
    """

    def __init__(self, num_thread: int = 3):
        self.executor = ThreadPoolExecutor(
            num_thread, thread_name_prefix="sqs-metrics-cloudwatch-dispatcher"
        )
        self.running = True

    def shutdown(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.running = False

    def dispatch_sqs_metric(
        self,
        account_id: str,
        region: str,
        queue_name: str,
        metric: str,
        value: float = 1,
        unit: str = "Count",
    ):
        """
        Publishes a metric to Cloudwatch using a Threadpool
        :param account_id The account id that should be used for Cloudwatch client
        :param region The region that should be used for Cloudwatch client
        :param queue_name The name of the queue that the metric belongs to
        :param metric The name of the metric
        :param value The value for that metric, default 1
        :param unit The unit for the value, default "Count"
        """
        if not self.running:
            return

        self.executor.submit(
            publish_sqs_metric,
            account_id=account_id,
            region=region,
            queue_name=queue_name,
            metric=metric,
            value=value,
            unit=unit,
        )

    def dispatch_metric_message_sent(self, queue: SqsQueue, message_body_size: int):
        """
        Sends metric 'NumberOfMessagesSent' and 'SentMessageSize' to Cloudwatch
        :param queue The Queue for which the metric will be send
        :param message_body_size the size of the message in bytes
        """
        self.dispatch_sqs_metric(
            account_id=queue.account_id,
            region=queue.region,
            queue_name=queue.name,
            metric="NumberOfMessagesSent",
        )
        self.dispatch_sqs_metric(
            account_id=queue.account_id,
            region=queue.region,
            queue_name=queue.name,
            metric="SentMessageSize",
            value=message_body_size,
            unit="Bytes",
        )

    def dispatch_metric_message_deleted(self, queue: SqsQueue, deleted: int = 1):
        """
        Sends metric 'NumberOfMessagesDeleted' to Cloudwatch
        :param queue The Queue for which the metric will be sent
        :param deleted The number of messages that were successfully deleted, default: 1
        """
        self.dispatch_sqs_metric(
            account_id=queue.account_id,
            region=queue.region,
            queue_name=queue.name,
            metric="NumberOfMessagesDeleted",
            value=deleted,
        )

    def dispatch_metric_received(self, queue: SqsQueue, received: int):
        """
        Sends metric 'NumberOfMessagesReceived' (if received > 0), or 'NumberOfEmptyReceives' to Cloudwatch
        :param queue The Queue for which the metric will be send
        :param received The number of messages that have been received
        """
        if received > 0:
            self.dispatch_sqs_metric(
                account_id=queue.account_id,
                region=queue.region,
                queue_name=queue.name,
                metric="NumberOfMessagesReceived",
                value=received,
            )
        else:
            self.dispatch_sqs_metric(
                account_id=queue.account_id,
                region=queue.region,
                queue_name=queue.name,
                metric="NumberOfEmptyReceives",
            )


class CloudwatchPublishWorker:
    """
    Regularly publish metrics data about approximate messages to Cloudwatch.
    Includes: ApproximateNumberOfMessagesVisible, ApproximateNumberOfMessagesNotVisible
        and ApproximateNumberOfMessagesDelayed
    """

    def __init__(self) -> None:
        super().__init__()
        self.scheduler = Scheduler()
        self.thread: Optional[FuncThread] = None

    def publish_approximate_cloudwatch_metrics(self):
        """Publishes the metrics for ApproximateNumberOfMessagesVisible, ApproximateNumberOfMessagesNotVisible
        and ApproximateNumberOfMessagesDelayed to CloudWatch"""
        # TODO ApproximateAgeOfOldestMessage is missing
        #  https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html

        for account_id, region, store in sqs_stores.iter_stores():
            start = 0
            # we can include up to 1000 metric queries for one put-metric-data call
            #  and we currently include 3 metrics per queue
            batch_size = 300

            while start < len(store.queues):
                batch_data = []
                # Process the current batch
                for queue in islice(store.queues.values(), start, start + batch_size):
                    batch_data.append(
                        SqsMetricBatchData(
                            QueueName=queue.name,
                            MetricName="ApproximateNumberOfMessagesVisible",
                            Value=queue.approx_number_of_messages,
                        )
                    )
                    batch_data.append(
                        SqsMetricBatchData(
                            QueueName=queue.name,
                            MetricName="ApproximateNumberOfMessagesNotVisible",
                            Value=queue.approx_number_of_messages_not_visible,
                        )
                    )
                    batch_data.append(
                        SqsMetricBatchData(
                            QueueName=queue.name,
                            MetricName="ApproximateNumberOfMessagesDelayed",
                            Value=queue.approx_number_of_messages_delayed,
                        )
                    )

                publish_sqs_metric_batch(
                    account_id=account_id, region=region, sqs_metric_batch_data=batch_data
                )
                # Update for the next batch
                start += batch_size

    def start(self):
        if self.thread:
            return

        self.scheduler = Scheduler()
        self.scheduler.schedule(
            self.publish_approximate_cloudwatch_metrics,
            period=config.SQS_CLOUDWATCH_METRICS_REPORT_INTERVAL,
        )

        def _run(*_args):
            self.scheduler.run()

        self.thread = start_thread(_run, name="sqs-approx-metrics-cloudwatch-publisher")

    def stop(self):
        if self.scheduler:
            self.scheduler.close()

        if self.thread:
            self.thread.stop()

        self.thread = None
        self.scheduler = None


class QueueUpdateWorker:
    """
    Regularly re-queues inflight and delayed messages whose visibility timeout has expired or delay deadline has been
    reached.
    """

    def __init__(self) -> None:
        super().__init__()
        self.scheduler = Scheduler()
        self.thread: Optional[FuncThread] = None
        self.mutex = threading.RLock()

    def iter_queues(self) -> Iterable[SqsQueue]:
        for account_id, region, store in sqs_stores.iter_stores():
            for queue in store.queues.values():
                yield queue

    def do_update_all_queues(self):
        for queue in self.iter_queues():
            try:
                queue.requeue_inflight_messages()
            except Exception:
                LOG.exception("error re-queueing inflight messages")

            try:
                queue.enqueue_delayed_messages()
            except Exception:
                LOG.exception("error enqueueing delayed messages")

            if config.SQS_ENABLE_MESSAGE_RETENTION_PERIOD:
                try:
                    queue.remove_expired_messages()
                except Exception:
                    LOG.exception("error removing expired messages")

    def start(self):
        with self.mutex:
            if self.thread:
                return

            self.scheduler = Scheduler()
            self.scheduler.schedule(self.do_update_all_queues, period=1)

            def _run(*_args):
                self.scheduler.run()

            self.thread = start_thread(_run, name="sqs-queue-update-worker")

    def stop(self):
        with self.mutex:
            if self.scheduler:
                self.scheduler.close()

            if self.thread:
                self.thread.stop()

            self.thread = None
            self.scheduler = None


class MessageMoveTaskManager:
    """
    Manages and runs MessageMoveTasks.

    TODO: we should check how AWS really moves messages internally: do they use the API?
     it's hard to know how AWS really does moving of messages. there are a number of things we could do
     to understand it better, including creating a DLQ chain and letting move tasks fail to see whether
     move tasks cause message consuming and create receipt handles. for now, we're doing a middle-layer
     transactional move, foregoing the API layer but using receipt handles and transactions.

    TODO: restoring move tasks from persistence doesn't work, may be a fringe case though

    TODO: re-drive into multiple original source queues
    """

    def __init__(self, stores: AccountRegionBundle[SqsStore] = None) -> None:
        self.stores = stores or sqs_stores
        self.mutex = threading.RLock()
        self.move_tasks: dict[str, MessageMoveTask] = dict()
        self.executor = ThreadPoolExecutor(max_workers=100, thread_name_prefix="sqs-move-message")

    def submit(self, move_task: MessageMoveTask):
        with self.mutex:
            try:
                source_queue = self._get_queue_by_arn(move_task.source_arn)
                move_task.approximate_number_of_messages_to_move = (
                    source_queue.approx_number_of_messages
                )
                move_task.approximate_number_of_messages_moved = 0
                move_task.mark_started()
                self.move_tasks[move_task.task_id] = move_task
                self.executor.submit(self._run, move_task)
            except Exception as e:
                self._fail_task(move_task, e)
                raise

    def cancel(self, move_task: MessageMoveTask):
        with self.mutex:
            move_task.status = MessageMoveTaskStatus.CANCELLING
            move_task.cancel_event.set()

    def close(self):
        with self.mutex:
            for move_task in self.move_tasks.values():
                move_task.cancel_event.set()

            self.executor.shutdown(wait=False, cancel_futures=True)

    def _run(self, move_task: MessageMoveTask):
        try:
            if move_task.destination_arn:
                LOG.info(
                    "Move task started %s (%s -> %s)",
                    move_task.task_id,
                    move_task.source_arn,
                    move_task.destination_arn,
                )
            else:
                LOG.info(
                    "Move task started %s (%s -> original sources)",
                    move_task.task_id,
                    move_task.source_arn,
                )

            while not move_task.cancel_event.is_set():
                # look up queues for every message in case they are removed
                source_queue = self._get_queue_by_arn(move_task.source_arn)

                receive_result = source_queue.receive(num_messages=1, visibility_timeout=1)

                if receive_result.dead_letter_messages:
                    raise NotImplementedError("Cannot deal with DLQ chains in move tasks")

                if not receive_result.successful:
                    # queue empty, task done
                    break

                message = receive_result.successful[0]
                receipt_handle = receive_result.receipt_handles[0]

                if move_task.destination_arn:
                    target_queue = self._get_queue_by_arn(move_task.destination_arn)
                else:
                    # we assume that dead_letter_source_arn is set since the message comes from a DLQ
                    target_queue = self._get_queue_by_arn(message.dead_letter_queue_source_arn)

                target_queue.put(
                    message=message.message,
                    message_group_id=message.message_group_id,
                    message_deduplication_id=message.message_deduplication_id,
                )
                source_queue.remove(receipt_handle)
                move_task.approximate_number_of_messages_moved += 1

                if rate := move_task.max_number_of_messages_per_second:
                    move_task.cancel_event.wait(timeout=(1 / rate))

        except Exception as e:
            self._fail_task(move_task, e)
        else:
            if move_task.cancel_event.is_set():
                LOG.info("Move task cancelled %s", move_task.task_id)
                move_task.status = MessageMoveTaskStatus.CANCELLED
            else:
                LOG.info("Move task completed successfully %s", move_task.task_id)
                move_task.status = MessageMoveTaskStatus.COMPLETED

    def _get_queue_by_arn(self, queue_arn: str) -> SqsQueue:
        arn = parse_arn(queue_arn)
        return SqsProvider._require_queue(arn["account"], arn["region"], arn["resource"])

    def _fail_task(self, task: MessageMoveTask, reason: Exception):
        """
        Marks a given task as failed due to the given reason.

        :param task: the task to mark as failed
        :param reason: the failure reason
        """
        LOG.info(
            "Exception occurred during move task %s: %s",
            task.task_id,
            reason,
            exc_info=LOG.isEnabledFor(logging.DEBUG),
        )
        task.status = MessageMoveTaskStatus.FAILED
        if isinstance(reason, ServiceException):
            task.failure_reason = reason.code
        else:
            task.failure_reason = reason.__class__.__name__


def check_attributes(message_attributes: MessageBodyAttributeMap):
    if not message_attributes:
        return
    for attribute_name in message_attributes:
        if len(attribute_name) >= 256:
            raise InvalidParameterValueException(
                "Message (user) attribute names must be shorter than 256 Bytes"
            )
        if not re.match(sqs_constants.ATTR_NAME_CHAR_REGEX, attribute_name.lower()):
            raise InvalidParameterValueException(
                "Message (user) attributes name can only contain upper and lower score characters, digits, periods, "
                "hyphens and underscores. "
            )
        if not re.match(sqs_constants.ATTR_NAME_PREFIX_SUFFIX_REGEX, attribute_name.lower()):
            raise InvalidParameterValueException(
                "You can't use message attribute names beginning with 'AWS.' or 'Amazon.'. "
                "These strings are reserved for internal use. Additionally, they cannot start or end with '.'."
            )

        attribute = message_attributes[attribute_name]
        attribute_type = attribute.get("DataType")
        if not attribute_type:
            raise InvalidParameterValueException("Missing required parameter DataType")
        if not re.match(sqs_constants.ATTR_TYPE_REGEX, attribute_type):
            raise InvalidParameterValueException(
                f"Type for parameter MessageAttributes.Attribute_name.DataType must be prefixed"
                f'with "String", "Binary", or "Number", but was: {attribute_type}'
            )
        if len(attribute_type) >= 256:
            raise InvalidParameterValueException(
                "Message (user) attribute types must be shorter than 256 Bytes"
            )

        if attribute_type == "String":
            try:
                attribute_value = attribute.get("StringValue")

                if not attribute_value:
                    raise InvalidParameterValueException(
                        f"Message (user) attribute '{attribute_name}' must contain a non-empty value of type 'String'."
                    )

                check_message_content(attribute_value)
            except InvalidMessageContents as e:
                # AWS throws a different exception here
                raise InvalidParameterValueException(e.args[0])


def check_fifo_id(fifo_id: str | None, parameter: str):
    if fifo_id is None:
        return
    if not re.match(sqs_constants.FIFO_MSG_REGEX, fifo_id):
        raise InvalidParameterValueException(
            f"Value {fifo_id} for parameter {parameter} is invalid. "
            f"Reason: {parameter} can only include alphanumeric and punctuation characters. 1 to 128 in length."
        )


def get_sqs_protocol(request: Request) -> Literal["query", "json"]:
    content_type = request.headers.get("Content-Type")
    return "json" if content_type == "application/x-amz-json-1.0" else "query"


def sqs_auto_protocol_aws_response_serializer(service_name: str, operation: str):
    def _decorate(fn):
        def _proxy(*args, **kwargs):
            # extract request from function invocation (decorator can be used for methods as well as for functions).
            if len(args) > 0 and isinstance(args[0], WerkzeugRequest):
                # function
                request = args[0]
            elif len(args) > 1 and isinstance(args[1], WerkzeugRequest):
                # method (arg[0] == self)
                request = args[1]
            elif "request" in kwargs:
                request = kwargs["request"]
            else:
                raise ValueError(f"could not find Request in signature of function {fn}")

            protocol = get_sqs_protocol(request)
            return aws_response_serializer(service_name, operation, protocol)(fn)(*args, **kwargs)

        return _proxy

    return _decorate


class SqsDeveloperEndpoints:
    """
    A set of SQS developer tool endpoints:

    - ``/_aws/sqs/messages``: list SQS messages without side effects, compatible with ``ReceiveMessage``.
    """

    def __init__(self, stores=None):
        self.stores = stores or sqs_stores

    @route("/_aws/sqs/messages", methods=["GET", "POST"])
    @sqs_auto_protocol_aws_response_serializer("sqs", "ReceiveMessage")
    def list_messages(self, request: Request) -> ReceiveMessageResult:
        """
        This endpoint expects a ``QueueUrl`` request parameter (either as query arg or form parameter), similar to
        the ``ReceiveMessage`` operation. It will parse the Queue URL generated by one of the SQS endpoint strategies.
        """

        if "x-amz-" in request.mimetype or "x-www-form-urlencoded" in request.mimetype:
            # only parse the request using a parser if it comes from an AWS client
            protocol = get_sqs_protocol(request)
            operation, service_request = create_parser(
                load_service("sqs", protocol=protocol)
            ).parse(request)
            if operation.name != "ReceiveMessage":
                raise CommonServiceException(
                    "InvalidRequest", "This endpoint only accepts ReceiveMessage calls"
                )
        else:
            service_request = dict(request.values)

        if not service_request.get("QueueUrl"):
            raise QueueDoesNotExist()

        try:
            account_id, region, queue_name = parse_queue_url(service_request.get("QueueUrl"))
        except ValueError:
            LOG.exception(
                "Error while parsing Queue URL from request values: %s", service_request.get
            )
            raise InvalidAddress()

        if not region:
            region = extract_region_from_headers(request.headers)

        return self._get_and_serialize_messages(request, region, account_id, queue_name)

    @route("/_aws/sqs/messages/<region>/<account_id>/<queue_name>")
    @sqs_auto_protocol_aws_response_serializer("sqs", "ReceiveMessage")
    def list_messages_for_queue_url(
        self, request: Request, region: str, account_id: str, queue_name: str
    ) -> ReceiveMessageResult:
        """
        This endpoint extracts the region, account_id, and queue_name directly from the URL rather than requiring the
        QueueUrl as parameter.
        """
        return self._get_and_serialize_messages(request, region, account_id, queue_name)

    def _get_and_serialize_messages(
        self,
        request: Request,
        region: str,
        account_id: str,
        queue_name: str,
    ) -> ReceiveMessageResult:
        show_invisible = request.values.get("ShowInvisible", "").lower() in ["true", "1"]
        show_delayed = request.values.get("ShowDelayed", "").lower() in ["true", "1"]

        try:
            store = SqsProvider.get_store(account_id, region)
            queue = store.queues[queue_name]
        except KeyError:
            LOG.info(
                "no queue named %s in region %s and account %s", queue_name, region, account_id
            )
            raise QueueDoesNotExist()

        messages = self._collect_messages(
            queue, show_invisible=show_invisible, show_delayed=show_delayed
        )

        return ReceiveMessageResult(Messages=messages)

    def _collect_messages(
        self, queue: SqsQueue, show_invisible: bool = False, show_delayed: bool = False
    ) -> List[Message]:
        """
        Retrieves from a given SqsQueue all visible messages without causing any side effects (not setting any
        receive timestamps, receive counts, or visibility state).

        :param queue: the queue
        :param show_invisible: show invisible messages as well
        :param show_delayed: show delayed messages as well
        :return: a list of messages
        """
        receipt_handle = "SQS/BACKDOOR/ACCESS"  # dummy receipt handle

        sqs_messages: List[SqsMessage] = []

        if show_invisible:
            sqs_messages.extend(queue.inflight)

        if isinstance(queue, StandardQueue):
            sqs_messages.extend(queue.visible.queue)
        elif isinstance(queue, FifoQueue):
            for message_group in queue.message_groups.values():
                for sqs_message in message_group.messages:
                    sqs_messages.append(sqs_message)
        else:
            raise ValueError(f"unknown queue type {type(queue)}")

        if show_delayed:
            sqs_messages.extend(queue.delayed)

        messages = []

        for sqs_message in sqs_messages:
            message: Message = to_sqs_api_message(sqs_message, [QueueAttributeName.All], ["All"])
            # these are all non-standard fields so we squelch the linter
            if show_invisible:
                message["Attributes"]["IsVisible"] = str(sqs_message.is_visible).lower()  # noqa
            if show_delayed:
                message["Attributes"]["IsDelayed"] = str(sqs_message.is_delayed).lower()  # noqa
            messages.append(message)
            message["ReceiptHandle"] = receipt_handle

        return messages


class SqsProvider(SqsApi, ServiceLifecycleHook):
    """
    LocalStack SQS Provider.

    LIMITATIONS:
        - Pagination of results (NextToken)
        - Delivery guarantees
        - The region is not encoded in the queue URL

    CROSS-ACCOUNT ACCESS:
    LocalStack permits cross-account access for all operations. However, AWS
    disallows the same for following operations:
        - AddPermission
        - CreateQueue
        - DeleteQueue
        - ListQueues
        - ListQueueTags
        - RemovePermission
        - SetQueueAttributes
        - TagQueue
        - UntagQueue
    """

    queues: Dict[str, SqsQueue]

    def __init__(self) -> None:
        super().__init__()
        self._queue_update_worker = QueueUpdateWorker()
        self._message_move_task_manager = MessageMoveTaskManager()
        self._router_rules = []
        self._init_cloudwatch_metrics_reporting()

    @staticmethod
    def get_store(account_id: str, region: str) -> SqsStore:
        return sqs_stores[account_id][region]

    def on_before_start(self):
        query_api.register(ROUTER)
        self._router_rules = ROUTER.add(SqsDeveloperEndpoints())
        self._queue_update_worker.start()
        self._start_cloudwatch_metrics_reporting()

    def on_before_stop(self):
        ROUTER.remove(self._router_rules)

        self._queue_update_worker.stop()
        self._message_move_task_manager.close()
        for _, _, store in sqs_stores.iter_stores():
            for queue in store.queues.values():
                queue.shutdown()

        self._stop_cloudwatch_metrics_reporting()

    @staticmethod
    def _require_queue(
        account_id: str, region_name: str, name: str, is_query: bool = False
    ) -> SqsQueue:
        """
        Returns the queue for the given name, or raises QueueDoesNotExist if it does not exist.

        :param: context: the request context
        :param name: the name to look for
        :param is_query: whether the request is using query protocol (error message is different)
        :returns: the queue
        :raises QueueDoesNotExist: if the queue does not exist
        """
        store = SqsProvider.get_store(account_id, region_name)
        with _STORE_LOCK:
            if name not in store.queues:
                if is_query:
                    message = "The specified queue does not exist for this wsdl version."
                else:
                    message = "The specified queue does not exist."
                raise QueueDoesNotExist(message)

            return store.queues[name]

    def _require_queue_by_arn(self, context: RequestContext, queue_arn: str) -> SqsQueue:
        arn = parse_arn(queue_arn)
        return self._require_queue(
            arn["account"],
            arn["region"],
            arn["resource"],
            is_query=context.service.protocol == "query",
        )

    def _resolve_queue(
        self,
        context: RequestContext,
        queue_name: Optional[str] = None,
        queue_url: Optional[str] = None,
    ) -> SqsQueue:
        """
        Determines the name of the queue from available information (request context, queue URL) to return the respective queue,
        or raises QueueDoesNotExist if it does not exist.

        :param context: the request context, used for getting region and account_id, and optionally the queue_url
        :param queue_name: the queue name (if this is set, then this will be used for the key)
        :param queue_url: the queue url (if name is not set, this will be used to determine the queue name)
        :returns: the queue
        :raises QueueDoesNotExist: if the queue does not exist
        """
        account_id, region_name, name = resolve_queue_location(context, queue_name, queue_url)
        is_query = context.service.protocol == "query"
        return self._require_queue(
            account_id, region_name or context.region, name, is_query=is_query
        )

    def create_queue(
        self,
        context: RequestContext,
        queue_name: String,
        attributes: QueueAttributeMap = None,
        tags: TagMap = None,
        **kwargs,
    ) -> CreateQueueResult:
        fifo = attributes and (
            attributes.get(QueueAttributeName.FifoQueue, "false").lower() == "true"
        )

        # Special Case TODO: why is an emtpy policy passed at all? same in set_queue_attributes
        if attributes and attributes.get(QueueAttributeName.Policy) == "":
            del attributes[QueueAttributeName.Policy]

        store = self.get_store(context.account_id, context.region)

        with _STORE_LOCK:
            if queue_name in store.queues:
                queue = store.queues[queue_name]

                if attributes:
                    # if attributes are set, then we check whether the existing attributes match the passed ones
                    queue.validate_queue_attributes(attributes)
                    for k, v in attributes.items():
                        if queue.attributes.get(k) != v:
                            LOG.debug(
                                "queue attribute values %s for queue %s do not match %s (existing) != %s (new)",
                                k,
                                queue_name,
                                queue.attributes.get(k),
                                v,
                            )
                            raise QueueNameExists(
                                f"A queue already exists with the same name and a different value for attribute {k}"
                            )

                return CreateQueueResult(QueueUrl=queue.url(context))

            if config.SQS_DELAY_RECENTLY_DELETED:
                deleted = store.deleted.get(queue_name)
                if deleted and deleted > (time.time() - sqs_constants.RECENTLY_DELETED_TIMEOUT):
                    raise QueueDeletedRecently(
                        "You must wait 60 seconds after deleting a queue before you can create "
                        "another with the same name."
                    )
            store.expire_deleted()

            # create the appropriate queue
            if fifo:
                queue = FifoQueue(queue_name, context.region, context.account_id, attributes, tags)
            else:
                queue = StandardQueue(
                    queue_name, context.region, context.account_id, attributes, tags
                )

            LOG.debug("creating queue key=%s attributes=%s tags=%s", queue_name, attributes, tags)

            store.queues[queue_name] = queue

        return CreateQueueResult(QueueUrl=queue.url(context))

    def get_queue_url(
        self,
        context: RequestContext,
        queue_name: String,
        queue_owner_aws_account_id: String = None,
        **kwargs,
    ) -> GetQueueUrlResult:
        queue = self._require_queue(
            queue_owner_aws_account_id or context.account_id,
            context.region,
            queue_name,
            is_query=context.service.protocol == "query",
        )

        return GetQueueUrlResult(QueueUrl=queue.url(context))

    def list_queues(
        self,
        context: RequestContext,
        queue_name_prefix: String = None,
        next_token: Token = None,
        max_results: BoxedInteger = None,
        **kwargs,
    ) -> ListQueuesResult:
        store = self.get_store(context.account_id, context.region)

        if queue_name_prefix:
            urls = [
                queue.url(context)
                for queue in store.queues.values()
                if queue.name.startswith(queue_name_prefix)
            ]
        else:
            urls = [queue.url(context) for queue in store.queues.values()]

        paginated_list = PaginatedList(urls)

        page_size = max_results if max_results else MAX_RESULT_LIMIT
        paginated_urls, next_token = paginated_list.get_page(
            token_generator=token_generator, next_token=next_token, page_size=page_size
        )

        if len(urls) == 0:
            return ListQueuesResult()

        return ListQueuesResult(QueueUrls=paginated_urls, NextToken=next_token)

    def change_message_visibility(
        self,
        context: RequestContext,
        queue_url: String,
        receipt_handle: String,
        visibility_timeout: NullableInteger,
        **kwargs,
    ) -> None:
        queue = self._resolve_queue(context, queue_url=queue_url)
        queue.update_visibility_timeout(receipt_handle, visibility_timeout)

    def change_message_visibility_batch(
        self,
        context: RequestContext,
        queue_url: String,
        entries: ChangeMessageVisibilityBatchRequestEntryList,
        **kwargs,
    ) -> ChangeMessageVisibilityBatchResult:
        queue = self._resolve_queue(context, queue_url=queue_url)

        self._assert_batch(entries)

        successful = []
        failed = []

        with queue.mutex:
            for entry in entries:
                try:
                    queue.update_visibility_timeout(
                        entry["ReceiptHandle"], entry["VisibilityTimeout"]
                    )
                    successful.append({"Id": entry["Id"]})
                except Exception as e:
                    failed.append(
                        BatchResultErrorEntry(
                            Id=entry["Id"],
                            SenderFault=False,
                            Code=e.__class__.__name__,
                            Message=str(e),
                        )
                    )

        return ChangeMessageVisibilityBatchResult(
            Successful=successful,
            Failed=failed,
        )

    def delete_queue(self, context: RequestContext, queue_url: String, **kwargs) -> None:
        account_id, region, name = parse_queue_url(queue_url)
        if region is None:
            region = context.region

        if account_id != context.account_id:
            LOG.warning(
                "Attempting a cross-account DeleteQueue operation (account from context: %s, account from queue url: %s, which is not allowed in AWS",
                account_id,
                context.account_id,
            )

        with _STORE_LOCK:
            store = self.get_store(account_id, region)
            queue = self._resolve_queue(context, queue_url=queue_url)
            LOG.debug(
                "deleting queue name=%s, region=%s, account=%s",
                queue.name,
                queue.region,
                queue.account_id,
            )
            # Trigger a shutdown prior to removing the queue resource
            store.queues[queue.name].shutdown()
            del store.queues[queue.name]
            store.deleted[queue.name] = time.time()

    def get_queue_attributes(
        self,
        context: RequestContext,
        queue_url: String,
        attribute_names: AttributeNameList = None,
        **kwargs,
    ) -> GetQueueAttributesResult:
        queue = self._resolve_queue(context, queue_url=queue_url)
        result = queue.get_queue_attributes(attribute_names=attribute_names)

        return GetQueueAttributesResult(Attributes=(result if result else None))

    def send_message(
        self,
        context: RequestContext,
        queue_url: String,
        message_body: String,
        delay_seconds: NullableInteger = None,
        message_attributes: MessageBodyAttributeMap = None,
        message_system_attributes: MessageBodySystemAttributeMap = None,
        message_deduplication_id: String = None,
        message_group_id: String = None,
        **kwargs,
    ) -> SendMessageResult:
        queue = self._resolve_queue(context, queue_url=queue_url)

        queue_item = self._put_message(
            queue,
            context,
            message_body,
            delay_seconds,
            message_attributes,
            message_system_attributes,
            message_deduplication_id,
            message_group_id,
        )
        message = queue_item.message
        return SendMessageResult(
            MessageId=message["MessageId"],
            MD5OfMessageBody=message["MD5OfBody"],
            MD5OfMessageAttributes=message.get("MD5OfMessageAttributes"),
            SequenceNumber=queue_item.sequence_number,
            MD5OfMessageSystemAttributes=_create_message_attribute_hash(message_system_attributes),
        )

    def send_message_batch(
        self,
        context: RequestContext,
        queue_url: String,
        entries: SendMessageBatchRequestEntryList,
        **kwargs,
    ) -> SendMessageBatchResult:
        queue = self._resolve_queue(context, queue_url=queue_url)

        self._assert_batch(
            entries,
            require_fifo_queue_params=is_fifo_queue(queue),
            require_message_deduplication_id=is_message_deduplication_id_required(queue),
        )
        # check the total batch size first and raise BatchRequestTooLong id > DEFAULT_MAXIMUM_MESSAGE_SIZE.
        # This is checked before any messages in the batch are sent.  Raising the exception here should
        # cause error response, rather than batching error results and returning
        self._assert_valid_batch_size(entries, sqs_constants.DEFAULT_MAXIMUM_MESSAGE_SIZE)

        successful = []
        failed = []

        with queue.mutex:
            for entry in entries:
                try:
                    queue_item = self._put_message(
                        queue,
                        context,
                        message_body=entry.get("MessageBody"),
                        delay_seconds=entry.get("DelaySeconds"),
                        message_attributes=entry.get("MessageAttributes"),
                        message_system_attributes=entry.get("MessageSystemAttributes"),
                        message_deduplication_id=entry.get("MessageDeduplicationId"),
                        message_group_id=entry.get("MessageGroupId"),
                    )
                    message = queue_item.message

                    successful.append(
                        SendMessageBatchResultEntry(
                            Id=entry["Id"],
                            MessageId=message.get("MessageId"),
                            MD5OfMessageBody=message.get("MD5OfBody"),
                            MD5OfMessageAttributes=message.get("MD5OfMessageAttributes"),
                            MD5OfMessageSystemAttributes=_create_message_attribute_hash(
                                message.get("message_system_attributes")
                            ),
                            SequenceNumber=queue_item.sequence_number,
                        )
                    )
                except ServiceException as e:
                    failed.append(
                        BatchResultErrorEntry(
                            Id=entry["Id"],
                            SenderFault=e.sender_fault,
                            Code=e.code,
                            Message=e.message,
                        )
                    )
                except Exception as e:
                    failed.append(
                        BatchResultErrorEntry(
                            Id=entry["Id"],
                            SenderFault=False,
                            Code=e.__class__.__name__,
                            Message=str(e),
                        )
                    )

        return SendMessageBatchResult(
            Successful=(successful if successful else None),
            Failed=(failed if failed else None),
        )

    def _put_message(
        self,
        queue: SqsQueue,
        context: RequestContext,
        message_body: String,
        delay_seconds: NullableInteger = None,
        message_attributes: MessageBodyAttributeMap = None,
        message_system_attributes: MessageBodySystemAttributeMap = None,
        message_deduplication_id: String = None,
        message_group_id: String = None,
    ) -> SqsMessage:
        check_message_min_size(message_body)
        check_message_max_size(message_body, message_attributes, queue.maximum_message_size)
        check_message_content(message_body)
        check_attributes(message_attributes)
        check_attributes(message_system_attributes)
        check_fifo_id(message_deduplication_id, "MessageDeduplicationId")
        check_fifo_id(message_group_id, "MessageGroupId")

        message = Message(
            MessageId=generate_message_id(),
            MD5OfBody=md5(message_body),
            Body=message_body,
            Attributes=self._create_message_attributes(context, message_system_attributes),
            MD5OfMessageAttributes=_create_message_attribute_hash(message_attributes),
            MessageAttributes=message_attributes,
        )
        if self._cloudwatch_dispatcher:
            self._cloudwatch_dispatcher.dispatch_metric_message_sent(
                queue=queue, message_body_size=len(message_body.encode("utf-8"))
            )

        return queue.put(
            message=message,
            message_deduplication_id=message_deduplication_id,
            message_group_id=message_group_id,
            delay_seconds=int(delay_seconds) if delay_seconds is not None else None,
        )

    def receive_message(
        self,
        context: RequestContext,
        queue_url: String,
        attribute_names: AttributeNameList = None,
        message_system_attribute_names: MessageSystemAttributeList = None,
        message_attribute_names: MessageAttributeNameList = None,
        max_number_of_messages: NullableInteger = None,
        visibility_timeout: NullableInteger = None,
        wait_time_seconds: NullableInteger = None,
        receive_request_attempt_id: String = None,
        **kwargs,
    ) -> ReceiveMessageResult:
        # TODO add support for message_system_attribute_names
        queue = self._resolve_queue(context, queue_url=queue_url)

        poll_empty_queue = False
        if override := extract_wait_time_seconds_from_headers(context):
            wait_time_seconds = override
            poll_empty_queue = True
        elif wait_time_seconds is None:
            wait_time_seconds = queue.wait_time_seconds
        elif wait_time_seconds < 0 or wait_time_seconds > 20:
            raise InvalidParameterValueException(
                f"Value {wait_time_seconds} for parameter WaitTimeSeconds is invalid. "
                f"Reason: Must be >= 0 and <= 20, if provided."
            )
        num = max_number_of_messages or 1

        # override receive count with value from custom header
        if override := extract_message_count_from_headers(context):
            num = override
        elif num == -1:
            # backdoor to get all messages
            num = queue.approx_number_of_messages
        elif (
            num < 1 or num > MAX_NUMBER_OF_MESSAGES
        ) and not SQS_DISABLE_MAX_NUMBER_OF_MESSAGE_LIMIT:
            raise InvalidParameterValueException(
                f"Value {num} for parameter MaxNumberOfMessages is invalid. "
                f"Reason: Must be between 1 and 10, if provided."
            )

        # we chose to always return the maximum possible number of messages, even though AWS will typically return
        # fewer messages than requested on small queues. at some point we could maybe change this to randomly sample
        # between 1 and max_number_of_messages.
        # see https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ReceiveMessage.html
        result = queue.receive(
            num, wait_time_seconds, visibility_timeout, poll_empty_queue=poll_empty_queue
        )

        # process dead letter messages
        if result.dead_letter_messages:
            dead_letter_target_arn = queue.redrive_policy["deadLetterTargetArn"]
            dl_queue = self._require_queue_by_arn(context, dead_letter_target_arn)
            # TODO: does this need to be atomic?
            for standard_message in result.dead_letter_messages:
                message = standard_message.message
                message["Attributes"][MessageSystemAttributeName.DeadLetterQueueSourceArn] = (
                    queue.arn
                )
                dl_queue.put(
                    message=message,
                    message_deduplication_id=standard_message.message_deduplication_id,
                    message_group_id=standard_message.message_group_id,
                )

                if isinstance(queue, FifoQueue):
                    message_group = queue.get_message_group(standard_message.message_group_id)
                    queue.update_message_group_visibility(message_group)

        # prepare result
        messages = []
        message_system_attribute_names = message_system_attribute_names or attribute_names
        for i, standard_message in enumerate(result.successful):
            message = to_sqs_api_message(
                standard_message, message_system_attribute_names, message_attribute_names
            )
            message["ReceiptHandle"] = result.receipt_handles[i]
            messages.append(message)

        if self._cloudwatch_dispatcher:
            self._cloudwatch_dispatcher.dispatch_metric_received(queue, received=len(messages))

        # TODO: how does receiving behave if the queue was deleted in the meantime?
        return ReceiveMessageResult(Messages=(messages if messages else None))

    def list_dead_letter_source_queues(
        self,
        context: RequestContext,
        queue_url: String,
        next_token: Token = None,
        max_results: BoxedInteger = None,
        **kwargs,
    ) -> ListDeadLetterSourceQueuesResult:
        urls = []
        store = self.get_store(context.account_id, context.region)
        dead_letter_queue = self._resolve_queue(context, queue_url=queue_url)
        for queue in store.queues.values():
            if policy := queue.redrive_policy:
                if policy.get("deadLetterTargetArn") == dead_letter_queue.arn:
                    urls.append(queue.url(context))
        return ListDeadLetterSourceQueuesResult(queueUrls=urls)

    def delete_message(
        self, context: RequestContext, queue_url: String, receipt_handle: String, **kwargs
    ) -> None:
        queue = self._resolve_queue(context, queue_url=queue_url)
        queue.remove(receipt_handle)
        if self._cloudwatch_dispatcher:
            self._cloudwatch_dispatcher.dispatch_metric_message_deleted(queue)

    def delete_message_batch(
        self,
        context: RequestContext,
        queue_url: String,
        entries: DeleteMessageBatchRequestEntryList,
        **kwargs,
    ) -> DeleteMessageBatchResult:
        queue = self._resolve_queue(context, queue_url=queue_url)
        override = extract_message_count_from_headers(context)
        self._assert_batch(entries, max_messages_override=override)
        self._assert_valid_message_ids(entries)

        successful = []
        failed = []

        with queue.mutex:
            for entry in entries:
                try:
                    queue.remove(entry["ReceiptHandle"])
                    successful.append(DeleteMessageBatchResultEntry(Id=entry["Id"]))
                except Exception as e:
                    failed.append(
                        BatchResultErrorEntry(
                            Id=entry["Id"],
                            SenderFault=False,
                            Code=e.__class__.__name__,
                            Message=str(e),
                        )
                    )
        if self._cloudwatch_dispatcher:
            self._cloudwatch_dispatcher.dispatch_metric_message_deleted(
                queue, deleted=len(successful)
            )

        return DeleteMessageBatchResult(
            Successful=successful,
            Failed=failed,
        )

    def purge_queue(self, context: RequestContext, queue_url: String, **kwargs) -> None:
        queue = self._resolve_queue(context, queue_url=queue_url)

        with queue.mutex:
            if config.SQS_DELAY_PURGE_RETRY:
                if queue.purge_timestamp and (queue.purge_timestamp + 60) > time.time():
                    raise PurgeQueueInProgress(
                        f"Only one PurgeQueue operation on {queue.name} is allowed every 60 seconds.",
                        status_code=403,
                    )
            queue.purge_timestamp = time.time()
            queue.clear()

    def set_queue_attributes(
        self, context: RequestContext, queue_url: String, attributes: QueueAttributeMap, **kwargs
    ) -> None:
        queue = self._resolve_queue(context, queue_url=queue_url)

        if not attributes:
            return

        queue.validate_queue_attributes(attributes)

        for k, v in attributes.items():
            if k in sqs_constants.INTERNAL_QUEUE_ATTRIBUTES:
                raise InvalidAttributeName(f"Unknown Attribute {k}.")
            queue.attributes[k] = v

        # Special cases
        if queue.attributes.get(QueueAttributeName.Policy) == "":
            del queue.attributes[QueueAttributeName.Policy]

        redrive_policy = queue.attributes.get(QueueAttributeName.RedrivePolicy)
        if redrive_policy == "":
            del queue.attributes[QueueAttributeName.RedrivePolicy]
            return

        if redrive_policy:
            _redrive_policy = json.loads(redrive_policy)
            dl_target_arn = _redrive_policy.get("deadLetterTargetArn")
            max_receive_count = _redrive_policy.get("maxReceiveCount")
            # TODO: use the actual AWS responses
            if not dl_target_arn:
                raise InvalidParameterValueException(
                    "The required parameter 'deadLetterTargetArn' is missing"
                )
            if max_receive_count is None:
                raise InvalidParameterValueException(
                    "The required parameter 'maxReceiveCount' is missing"
                )
            try:
                max_receive_count = int(max_receive_count)
                valid_count = 1 <= max_receive_count <= 1000
            except ValueError:
                valid_count = False
            if not valid_count:
                raise InvalidParameterValueException(
                    f"Value {redrive_policy} for parameter RedrivePolicy is invalid. Reason: Invalid value for "
                    f"maxReceiveCount: {max_receive_count}, valid values are from 1 to 1000 both inclusive."
                )

    def list_message_move_tasks(
        self,
        context: RequestContext,
        source_arn: String,
        max_results: NullableInteger = None,
        **kwargs,
    ) -> ListMessageMoveTasksResult:
        try:
            self._require_queue_by_arn(context, source_arn)
        except InvalidArnException:
            raise InvalidParameterValueException(
                "You must use this format to specify the SourceArn: arn:<partition>:<service>:<region>:<account-id>:<resource-id>"
            )
        except QueueDoesNotExist:
            raise ResourceNotFoundException(
                "The resource that you specified for the SourceArn parameter doesn't exist."
            )

        # get move tasks for queue and sort them by most-recent
        store = self.get_store(context.account_id, context.region)
        tasks = [
            move_task
            for move_task in store.move_tasks.values()
            if move_task.source_arn == source_arn
            and move_task.status != MessageMoveTaskStatus.CREATED
        ]
        tasks.sort(key=lambda t: t.started_timestamp, reverse=True)

        # convert to result list
        n = max_results or 1
        return ListMessageMoveTasksResult(
            Results=[self._to_message_move_task_entry(task) for task in tasks[:n]]
        )

    def _to_message_move_task_entry(
        self, entity: MessageMoveTask
    ) -> ListMessageMoveTasksResultEntry:
        """
        Converts a ``MoveMessageTask`` entity into a ``ListMessageMoveTasksResultEntry`` API concept.

        :param entity: the entity to convert
        :return: the typed dict for use in the AWS API
        """
        entry = ListMessageMoveTasksResultEntry(
            SourceArn=entity.source_arn,
            DestinationArn=entity.destination_arn,
            Status=entity.status,
        )

        if entity.status == "RUNNING":
            entry["TaskHandle"] = entity.task_handle
        if entity.started_timestamp is not None:
            entry["StartedTimestamp"] = int(entity.started_timestamp.timestamp() * 1000)
        if entity.max_number_of_messages_per_second is not None:
            entry["MaxNumberOfMessagesPerSecond"] = entity.max_number_of_messages_per_second
        if entity.approximate_number_of_messages_to_move is not None:
            entry["ApproximateNumberOfMessagesToMove"] = (
                entity.approximate_number_of_messages_to_move
            )
        if entity.approximate_number_of_messages_moved is not None:
            entry["ApproximateNumberOfMessagesMoved"] = entity.approximate_number_of_messages_moved
        if entity.failure_reason is not None:
            entry["FailureReason"] = entity.failure_reason

        return entry

    def start_message_move_task(
        self,
        context: RequestContext,
        source_arn: String,
        destination_arn: String = None,
        max_number_of_messages_per_second: NullableInteger = None,
        **kwargs,
    ) -> StartMessageMoveTaskResult:
        try:
            self._require_queue_by_arn(context, source_arn)
        except QueueDoesNotExist as e:
            raise ResourceNotFoundException(
                "The resource that you specified for the SourceArn parameter doesn't exist.",
                status_code=404,
            ) from e

        # check that the source queue is the dlq of some other queue
        is_dlq = False
        for _, _, store in sqs_stores.iter_stores():
            for queue in store.queues.values():
                if not queue.redrive_policy:
                    continue
                if queue.redrive_policy.get("deadLetterTargetArn") == source_arn:
                    is_dlq = True
                    break
            if is_dlq:
                break
        if not is_dlq:
            raise InvalidParameterValueException(
                "Source queue must be configured as a Dead Letter Queue."
            )

        # If destination_arn is left blank, the messages will be redriven back to their respective original
        # source queues.
        if destination_arn:
            try:
                self._require_queue_by_arn(context, destination_arn)
            except QueueDoesNotExist as e:
                raise ResourceNotFoundException(
                    "The resource that you specified for the DestinationArn parameter doesn't exist.",
                    status_code=404,
                ) from e

        # check that only one active task exists
        with self._message_move_task_manager.mutex:
            store = self.get_store(context.account_id, context.region)
            tasks = [
                task
                for task in store.move_tasks.values()
                if task.source_arn == source_arn
                and task.status
                in [
                    MessageMoveTaskStatus.CREATED,
                    MessageMoveTaskStatus.RUNNING,
                    MessageMoveTaskStatus.CANCELLING,
                ]
            ]
            if len(tasks) > 0:
                raise InvalidParameterValueException(
                    "There is already a task running. Only one active task is allowed for a source queue "
                    "arn at a given time."
                )

            task = MessageMoveTask(
                source_arn,
                destination_arn,
                max_number_of_messages_per_second,
            )
            store.move_tasks[task.task_id] = task

        self._message_move_task_manager.submit(task)

        return StartMessageMoveTaskResult(TaskHandle=task.task_handle)

    def cancel_message_move_task(
        self, context: RequestContext, task_handle: String, **kwargs
    ) -> CancelMessageMoveTaskResult:
        try:
            task_id, source_arn = decode_move_task_handle(task_handle)
        except ValueError as e:
            raise InvalidParameterValueException(
                "Value for parameter TaskHandle is invalid."
            ) from e

        try:
            self._require_queue_by_arn(context, source_arn)
        except QueueDoesNotExist as e:
            raise ResourceNotFoundException(
                "The resource that you specified for the SourceArn parameter doesn't exist.",
                status_code=404,
            ) from e

        store = self.get_store(context.account_id, context.region)
        try:
            move_task = store.move_tasks[task_id]
        except KeyError:
            raise ResourceNotFoundException("Task does not exist.", status_code=404)

        # TODO: what happens if move tasks are already cancelled?

        self._message_move_task_manager.cancel(move_task)

        return CancelMessageMoveTaskResult(
            ApproximateNumberOfMessagesMoved=move_task.approximate_number_of_messages_moved,
        )

    def tag_queue(self, context: RequestContext, queue_url: String, tags: TagMap, **kwargs) -> None:
        queue = self._resolve_queue(context, queue_url=queue_url)

        if not tags:
            return

        for k, v in tags.items():
            queue.tags[k] = v

    def list_queue_tags(
        self, context: RequestContext, queue_url: String, **kwargs
    ) -> ListQueueTagsResult:
        queue = self._resolve_queue(context, queue_url=queue_url)
        return ListQueueTagsResult(Tags=(queue.tags if queue.tags else None))

    def untag_queue(
        self, context: RequestContext, queue_url: String, tag_keys: TagKeyList, **kwargs
    ) -> None:
        queue = self._resolve_queue(context, queue_url=queue_url)

        for k in tag_keys:
            if k in queue.tags:
                del queue.tags[k]

    def add_permission(
        self,
        context: RequestContext,
        queue_url: String,
        label: String,
        aws_account_ids: AWSAccountIdList,
        actions: ActionNameList,
        **kwargs,
    ) -> None:
        queue = self._resolve_queue(context, queue_url=queue_url)

        self._validate_actions(actions)

        queue.add_permission(label=label, actions=actions, account_ids=aws_account_ids)

    def remove_permission(
        self, context: RequestContext, queue_url: String, label: String, **kwargs
    ) -> None:
        queue = self._resolve_queue(context, queue_url=queue_url)

        queue.remove_permission(label=label)

    def _create_message_attributes(
        self,
        context: RequestContext,
        message_system_attributes: MessageBodySystemAttributeMap = None,
    ) -> Dict[MessageSystemAttributeName, str]:
        result: Dict[MessageSystemAttributeName, str] = {
            MessageSystemAttributeName.SenderId: context.account_id,  # not the account ID in AWS
            MessageSystemAttributeName.SentTimestamp: str(now(millis=True)),
        }
        # we are not using the `context.trace_context` here as it is automatically populated
        # AWS only adds the `AWSTraceHeader` attribute if the header is explicitly present
        # TODO: check maybe with X-Ray Active mode?
        if "X-Amzn-Trace-Id" in context.request.headers:
            result[MessageSystemAttributeName.AWSTraceHeader] = str(
                context.request.headers["X-Amzn-Trace-Id"]
            )

        if message_system_attributes is not None:
            for attr in message_system_attributes:
                result[attr] = message_system_attributes[attr]["StringValue"]

        return result

    def _validate_actions(self, actions: ActionNameList):
        service = load_service(service=self.service, version=self.version)
        # FIXME: this is a bit of a heuristic as it will also include actions like "ListQueues" which is not
        #  associated with an action on a queue
        valid = list(service.operation_names)
        valid.append("*")

        for action in actions:
            if action not in valid:
                raise InvalidParameterValueException(
                    f"Value SQS:{action} for parameter ActionName is invalid. Reason: Please refer to the appropriate "
                    "WSDL for a list of valid actions. "
                )

    def _assert_batch(
        self,
        batch: List,
        *,
        require_fifo_queue_params: bool = False,
        require_message_deduplication_id: bool = False,
        max_messages_override: int | None = None,
    ) -> None:
        if not batch:
            raise EmptyBatchRequest

        max_messages_per_batch = max_messages_override or MAX_NUMBER_OF_MESSAGES
        if batch and (no_entries := len(batch)) > max_messages_per_batch:
            raise TooManyEntriesInBatchRequest(
                f"Maximum number of entries per request are {max_messages_per_batch}. You have sent {no_entries}."
            )
        visited = set()
        for entry in batch:
            entry_id = entry["Id"]
            if not re.search(r"^[\w-]+$", entry_id) or len(entry_id) > 80:
                raise InvalidBatchEntryId(
                    "A batch entry id can only contain alphanumeric characters, hyphens and underscores. "
                    "It can be at most 80 letters long."
                )
            if require_message_deduplication_id and not entry.get("MessageDeduplicationId"):
                raise InvalidParameterValueException(
                    "The queue should either have ContentBasedDeduplication enabled or "
                    "MessageDeduplicationId provided explicitly"
                )
            if require_fifo_queue_params and not entry.get("MessageGroupId"):
                raise InvalidParameterValueException(
                    "The request must contain the parameter MessageGroupId."
                )
            if entry_id in visited:
                raise BatchEntryIdsNotDistinct()
            else:
                visited.add(entry_id)

    def _assert_valid_batch_size(self, batch: List, max_message_size: int):
        batch_message_size = sum(
            _message_body_size(entry.get("MessageBody"))
            + _message_attributes_size(entry.get("MessageAttributes"))
            for entry in batch
        )
        if batch_message_size > max_message_size:
            error = f"Batch requests cannot be longer than {max_message_size} bytes."
            error += f" You have sent {batch_message_size} bytes."
            raise BatchRequestTooLong(error)

    def _assert_valid_message_ids(self, batch: List):
        batch_id_regex = r"^[\w-]{1,80}$"
        for message in batch:
            if not re.match(batch_id_regex, message.get("Id", "")):
                raise InvalidBatchEntryId(
                    "A batch entry id can only contain alphanumeric characters, "
                    "hyphens and underscores. It can be at most 80 letters long."
                )

    def _init_cloudwatch_metrics_reporting(self):
        self.cloudwatch_disabled: bool = (
            config.SQS_DISABLE_CLOUDWATCH_METRICS or not is_api_enabled("cloudwatch")
        )

        self._cloudwatch_publish_worker = (
            None if self.cloudwatch_disabled else CloudwatchPublishWorker()
        )
        self._cloudwatch_dispatcher = None if self.cloudwatch_disabled else CloudwatchDispatcher()

    def _start_cloudwatch_metrics_reporting(self):
        if not self.cloudwatch_disabled:
            self._cloudwatch_publish_worker.start()

    def _stop_cloudwatch_metrics_reporting(self):
        if not self.cloudwatch_disabled:
            self._cloudwatch_publish_worker.stop()
            self._cloudwatch_dispatcher.shutdown()


# Method from moto's attribute_md5 of moto/sqs/models.py, separated from the Message Object
def _create_message_attribute_hash(message_attributes) -> Optional[str]:
    # To avoid the need to check for dict conformity everytime we invoke this function
    if not isinstance(message_attributes, dict):
        return
    hash = hashlib.md5()

    for attrName in sorted(message_attributes.keys()):
        attr_value = message_attributes[attrName]
        # Encode name
        MotoMessage.update_binary_length_and_value(hash, MotoMessage.utf8(attrName))
        # Encode data type
        MotoMessage.update_binary_length_and_value(hash, MotoMessage.utf8(attr_value["DataType"]))
        # Encode transport type and value
        if attr_value.get("StringValue"):
            hash.update(bytearray([STRING_TYPE_FIELD_INDEX]))
            MotoMessage.update_binary_length_and_value(
                hash, MotoMessage.utf8(attr_value.get("StringValue"))
            )
        elif attr_value.get("BinaryValue"):
            hash.update(bytearray([BINARY_TYPE_FIELD_INDEX]))
            decoded_binary_value = attr_value.get("BinaryValue")
            MotoMessage.update_binary_length_and_value(hash, decoded_binary_value)
        # string_list_value, binary_list_value type is not implemented, reserved for the future use.
        # See https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_MessageAttributeValue.html
    return hash.hexdigest()


def resolve_queue_location(
    context: RequestContext, queue_name: Optional[str] = None, queue_url: Optional[str] = None
) -> Tuple[str, Optional[str], str]:
    """
    Resolves a queue location from the given information.

    :param context: the request context, used for getting region and account_id, and optionally the queue_url
    :param queue_name: the queue name (if this is set, then this will be used for the key)
    :param queue_url: the queue url (if name is not set, this will be used to determine the queue name)
    :return: tuple of account id, region and queue_name
    """
    if not queue_name:
        try:
            if queue_url:
                return parse_queue_url(queue_url)
            else:
                return parse_queue_url(context.request.base_url)
        except ValueError:
            # should work if queue name is passed in QueueUrl
            return context.account_id, context.region, queue_url

    return context.account_id, context.region, queue_name


def to_sqs_api_message(
    standard_message: SqsMessage,
    attribute_names: AttributeNameList = None,
    message_attribute_names: MessageAttributeNameList = None,
) -> Message:
    """
    Utility function to convert an SQS message from LocalStack's internal representation to the AWS API
    concept 'Message', which is the format returned by the ``ReceiveMessage`` operation.

    :param standard_message: A LocalStack SQS message
    :param attribute_names: the attribute name list to filter
    :param message_attribute_names: the message attribute names to filter
    :return: a copy of the original Message with updated message attributes and MD5 attribute hash sums
    """
    # prepare message for receiver
    message = copy.deepcopy(standard_message.message)

    # update system attributes of the message copy
    message["Attributes"][MessageSystemAttributeName.ApproximateFirstReceiveTimestamp] = str(
        int((standard_message.first_received or 0) * 1000)
    )

    # filter attributes for receiver
    message_filter_attributes(message, attribute_names)
    message_filter_message_attributes(message, message_attribute_names)
    if message.get("MessageAttributes"):
        message["MD5OfMessageAttributes"] = _create_message_attribute_hash(
            message["MessageAttributes"]
        )
    else:
        # delete the value that was computed when creating the message
        message.pop("MD5OfMessageAttributes", None)
    return message


def message_filter_attributes(message: Message, names: Optional[AttributeNameList]):
    """
    Utility function filter from the given message (in-place) the system attributes from the given list. It will
    apply all rules according to:
    https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sqs.html#SQS.Client.receive_message.

    :param message: The message to filter (it will be modified)
    :param names: the attributes names/filters
    """
    if "Attributes" not in message:
        return

    if not names:
        del message["Attributes"]
        return

    if QueueAttributeName.All in names:
        return

    for k in list(message["Attributes"].keys()):
        if k not in names:
            del message["Attributes"][k]


def message_filter_message_attributes(message: Message, names: Optional[MessageAttributeNameList]):
    """
    Utility function filter from the given message (in-place) the message attributes from the given list. It will
    apply all rules according to:
    https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sqs.html#SQS.Client.receive_message.

    :param message: The message to filter (it will be modified)
    :param names: the attributes names/filters (can be 'All', '.*', '*' or prefix filters like 'Foo.*')
    """
    if not message.get("MessageAttributes"):
        return

    if not names:
        del message["MessageAttributes"]
        return

    if "All" in names or ".*" in names or "*" in names:
        return

    attributes = message["MessageAttributes"]
    matched = []

    keys = [name for name in names if ".*" not in name]
    prefixes = [name.split(".*")[0] for name in names if ".*" in name]

    # match prefix filters
    for k in attributes:
        if k in keys:
            matched.append(k)
            continue

        for prefix in prefixes:
            if k.startswith(prefix):
                matched.append(k)
            break
    if matched:
        message["MessageAttributes"] = {k: attributes[k] for k in matched}
    else:
        message.pop("MessageAttributes")


def extract_message_count_from_headers(context: RequestContext) -> int | None:
    if override := context.request.headers.get(
        HEADER_LOCALSTACK_SQS_OVERRIDE_MESSAGE_COUNT, default=None, type=int
    ):
        return override

    return None


def extract_wait_time_seconds_from_headers(context: RequestContext) -> int | None:
    if override := context.request.headers.get(
        HEADER_LOCALSTACK_SQS_OVERRIDE_WAIT_TIME_SECONDS, default=None, type=int
    ):
        return override

    return None
