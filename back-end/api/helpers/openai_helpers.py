import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

from pydantic import BaseModel
from openai import OpenAI, InternalServerError
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type


@dataclass
class CompatFunctionCall:
    name: str
    arguments: str

    def dict(self):
        return {
            'name': self.name,
            'arguments': self.arguments,
        }


@dataclass
class CompatToolCall:
    id: str
    function: CompatFunctionCall
    type: str = 'function'

    def dict(self):
        return {
            'id': self.id,
            'type': self.type,
            'function': self.function.dict(),
        }


@dataclass
class CompatAssistantMessage:
    role: str = 'assistant'
    content: str = ''
    tool_calls: List[CompatToolCall] = field(default_factory=list)

    def dict(self):
        payload = {
            'role': self.role,
            'content': self.content,
        }
        if self.tool_calls:
            payload['tool_calls'] = [tool_call.dict() for tool_call in self.tool_calls]
        return payload


@retry(retry=retry_if_exception_type(InternalServerError), stop=stop_after_attempt(10), wait=wait_fixed(2))
def query_responses_api(args):
    with OpenAI(timeout=180.0) as client:
        return client.responses.create(**args)


def create_response_message(messages, functions, temperature=1, model='gpt-5.4'):
    print('---')
    print(f'---Calling GPT {model}---')
    openai_args = {
        'model': model,
        'temperature': temperature,
        'input': _messages_to_responses_input(messages),
    }
    openai_args['tools'] = _functions_to_responses_tools(functions)
    response = query_responses_api(openai_args)
    print(
        '---Response'
        f' id={getattr(response, "id", "-")}'
        f' status={getattr(response, "status", "-")}'
        f' output_items={len(getattr(response, "output", []) or [])}'
        '---'
    )
    return _response_to_compat_message(response)


def _messages_to_responses_input(messages) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for message in messages:
        openai_obj = getattr(message, 'openai_obj', None) or {}
        role = str(openai_obj.get('role') or '').strip().lower()
        content_text = _normalize_content_to_text(openai_obj.get('content'))

        if role == 'tool':
            tool_call_id = openai_obj.get('tool_call_id')
            if tool_call_id:
                items.append({
                    'type': 'function_call_output',
                    'call_id': str(tool_call_id),
                    'output': content_text,
                })
            elif content_text:
                items.append({'role': 'user', 'content': content_text})
            continue

        if role == 'assistant':
            if content_text:
                items.append({'role': 'assistant', 'content': content_text})

            for tool_call in openai_obj.get('tool_calls') or []:
                function_obj = (tool_call or {}).get('function') or {}
                tool_call_id = (tool_call or {}).get('id')
                function_name = function_obj.get('name')
                arguments = _normalize_function_arguments(function_obj.get('arguments'))
                if tool_call_id and function_name:
                    items.append({
                        'type': 'function_call',
                        'call_id': str(tool_call_id),
                        'name': str(function_name),
                        'arguments': arguments,
                    })
            continue

        # system/user (and any unexpected role fallback)
        items.append({
            'role': role if role in {'system', 'user'} else 'user',
            'content': content_text,
        })

    return items


def _functions_to_responses_tools(functions) -> List[Dict[str, Any]]:
    tools = []
    for function_model in functions:
        schema = function_model.openai_schema()
        tools.append({
            'type': 'function',
            'name': schema['name'],
            'description': schema.get('description') or '',
            'parameters': schema['parameters'],
        })
    return tools


def _response_to_compat_message(response) -> CompatAssistantMessage:
    content_parts: List[str] = []
    tool_calls: List[CompatToolCall] = []

    for item in getattr(response, 'output', []) or []:
        item_type = getattr(item, 'type', '')
        if item_type == 'message':
            for content in getattr(item, 'content', []) or []:
                content_type = getattr(content, 'type', '')
                if content_type in {'output_text', 'text'}:
                    chunk = getattr(content, 'text', '')
                    if chunk:
                        content_parts.append(str(chunk))
        elif item_type == 'function_call':
            call_id = getattr(item, 'call_id', None) or getattr(item, 'id', None)
            name = getattr(item, 'name', None)
            if not call_id or not name:
                continue
            arguments = _normalize_function_arguments(getattr(item, 'arguments', ''))
            tool_calls.append(
                CompatToolCall(
                    id=str(call_id),
                    function=CompatFunctionCall(
                        name=str(name),
                        arguments=arguments,
                    ),
                )
            )

    content = '\n'.join(content_parts).strip()
    if not content and getattr(response, 'output_text', None):
        content = str(response.output_text).strip()

    return CompatAssistantMessage(content=content, tool_calls=tool_calls)


def _normalize_content_to_text(content: Any) -> str:
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for entry in content:
            if isinstance(entry, str):
                parts.append(entry)
                continue
            if isinstance(entry, dict):
                text = entry.get('text')
                if isinstance(text, str):
                    parts.append(text)
                    continue
            parts.append(json.dumps(entry, ensure_ascii=False))
        return '\n'.join(parts)
    if isinstance(content, dict):
        text = content.get('text')
        if isinstance(text, str):
            return text
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _normalize_function_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments or {}, ensure_ascii=False)
    except Exception:
        return str(arguments)

def custom_schema(cls: BaseModel) -> Dict[str, Any]:
    parameters = cls.schema()
    # Remove internal Pydantic helper fields we never want to expose
    parameters['properties'] = {
        k: v
        for k, v in parameters['properties'].items()
        if k not in ('v__duplicate_kwargs', 'args', 'kwargs')
    }

    # Pydantic already sets the correct list of required fields in the schema.  
    # Don't overwrite it with *all* properties (that turned every optional field into a required one).
    # We only ensure the key exists so OpenAI gets a well-formed JSON Schema.
    parameters['required'] = parameters.get('required', [])

    # Clean out noisy keys that inflate the schema size.
    for remove_key in ['title', 'additionalProperties', 'description']:
        _remove_a_key(parameters, remove_key)
    return {
        'name': cls.__name__,
        'description': cls.__doc__,
        'parameters': parameters,
    }


class OpenAIBaseModel(BaseModel):
    @classmethod
    def openai_schema(cls):
        return custom_schema(cls)

# def get_function(fn):
#     if fn.name.lower() == 'python' and fn.arguments.replace(' ', '')[:8] != '{"code":':
#         print('Python args not wrapped in code')
#         fn.arguments = {'code': fn.arguments}
#     else:
#         fn.arguments = json.loads(fn.arguments, strict=False) 
#     return fn

def _remove_a_key(d, remove_key) -> None:
    """Remove a key from a dictionary recursively"""
    if isinstance(d, dict):
        for key in list(d.keys()):
            if key == remove_key:
                del d[key]
            else:
                _remove_a_key(d[key], remove_key)
