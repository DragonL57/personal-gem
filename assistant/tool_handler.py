"""
Tool execution and processing
"""

import json
import inspect
from colorama import Fore, Style
from pydantic import BaseModel
from tools import validate_tool_call, tool_report_print
from assistant.api_client import preprocess_messages_for_litellm

def process_tool_calls(assistant, response_json, print_response=True, validation_retries=2, recursion_depth=0, tool_event_callback=None):
    """
    Process a response from the API, handling any tool calls and follow-up responses.
    
    This method recursively processes tool calls until no more are requested,
    allowing for multi-turn tool calls in a single conversation turn.
    
    Args:
        assistant: The Assistant instance
        response_json: The JSON response from the API
        print_response: Whether to print the final response to console
        validation_retries: Number of retries left for tool validation issues
        recursion_depth: Current recursion depth to prevent infinite recursion
        tool_event_callback: Optional callback function to handle tool call events
        
    Returns:
        Generator yielding events and finally a dict with the text response
    """
    # We'll still track recursion depth for debugging but won't limit it
    if tool_event_callback:
        for chunk in tool_event_callback("recursion_depth", recursion_depth):
            yield chunk
        
    if not response_json or "choices" not in response_json or not response_json["choices"]:
        print(f"{Fore.RED}Error: Invalid response format from API: {response_json}{Style.RESET_ALL}")
        yield {"final_text": "Error: Received invalid response from API."}
        return

    # Extract the message from the response
    response_message = response_json["choices"][0]["message"]
    
    # Add the message to our conversation history
    if response_message not in assistant.messages:
        assistant.messages.append(response_message)
    
    # Check if there are any tool calls in the response
    tool_calls = response_message.get("tool_calls", [])
    
    # If no tool calls, this is a regular response - print and return it
    if not tool_calls:
        if print_response and response_message.get("content"):
            assistant.print_ai(response_message.get("content"))
        yield {"final_text": response_message.get("content", "")}
        return
    
    # Initialize current_tool_calls if it doesn't exist yet
    if not hasattr(assistant, 'current_tool_calls'):
        assistant.current_tool_calls = []
    
    # Add this response's tool calls to our tracking list
    for tool_call in tool_calls:
        # Check if this tool call is already being tracked (prevent duplicates)
        tool_id = tool_call["id"]
        function_name = tool_call["function"]["name"]
        function_args = tool_call["function"]["arguments"]
        
        # Create a signature for deduplication
        tool_signature = f"{function_name}:{function_args}"
        
        # Check if we're already tracking this exact tool call
        is_duplicate = False
        for existing_tool in assistant.current_tool_calls:
            if existing_tool["name"] == function_name and existing_tool["args"] == function_args:
                is_duplicate = True
                break
        
        # Only add if it's not a duplicate
        if not is_duplicate:
            tool_data = {
                "id": tool_id,
                "name": function_name,
                "args": function_args,
                "status": "pending",
                "result": None
            }
            
            # Notify about the tool call via callback if provided
            if tool_event_callback:
                for chunk in tool_event_callback("tool_call", tool_data):
                    yield chunk
                    
            assistant.current_tool_calls.append(tool_data)
    
    # Print any message content that came with the tool calls
    if print_response and response_message.get("content"):
        print(f"{Fore.YELLOW}│ {Fore.GREEN}{assistant.name}:{Style.RESET_ALL} {Style.DIM}{Fore.WHITE}{response_message['content'].strip()}{Style.RESET_ALL}")
    
    # Process each tool call
    has_errors = False
    processed_tool_ids = set()  # Track which tool calls we've processed
    
    for tool_call in tool_calls:
        function_name = tool_call["function"]["name"]
        function_to_call = assistant.available_functions.get(function_name)
        tool_id = tool_call["id"]
        
        # Skip if we've already processed this tool ID to prevent duplicate execution
        if tool_id in processed_tool_ids:
            continue
            
        processed_tool_ids.add(tool_id)  # Mark as processed
        
        # Check if the function exists
        if function_to_call is None:
            err_msg = f"Function not found with name: {function_name}"
            print(f"{Fore.RED}Error: {err_msg}{Style.RESET_ALL}")
            
            # Update tool call with error status
            for tc in assistant.current_tool_calls:
                if tc["id"] == tool_id:
                    tc["status"] = "error"
                    tc["result"] = err_msg
                    # Send update via callback
                    if tool_event_callback:
                        for chunk in tool_event_callback("tool_update", tc):
                            yield chunk
                    break
                    
            assistant.add_toolcall_output(tool_id, function_name, err_msg)
            has_errors = True
            continue
        
        try:
            # Parse and validate arguments
            function_args_str = tool_call["function"]["arguments"]
            
            # Handle empty arguments by providing an empty object
            if not function_args_str.strip():
                function_args_str = "{}"
                
            function_args = json.loads(function_args_str)
            
            # Check for valid parameters
            is_valid, error_message = validate_tool_call(function_name, function_args)
            
            if not is_valid:
                # If validation fails, try to add missing required parameters
                if validation_retries > 0:
                    print(f"{Fore.YELLOW}Tool call validation failed: {error_message}. Retrying with corrected parameters.{Style.RESET_ALL}")
                    # Request a corrected tool call (implement this logic if needed)
                    # For now, just fail with the error message
                    err_msg = f"Error validating tool arguments: {error_message}"
                    print(f"{Fore.RED}Error: {err_msg}{Style.RESET_ALL}")
                    
                    # Update tool call with error status
                    for tc in assistant.current_tool_calls:
                        if tc["id"] == tool_id:
                            tc["status"] = "error"
                            tc["result"] = err_msg
                            # Send update via callback
                            if tool_event_callback:
                                for chunk in tool_event_callback("tool_update", tc):
                                    yield chunk
                            break
                    
                    assistant.add_toolcall_output(tool_id, function_name, err_msg)
                    has_errors = True
                    continue
            
            # Execute the tool in a controlled manner
            print(f"About to execute tool: {function_name}")
            try:
                # Signal tool execution start for real-time UI updates
                from tools.formatting import tool_report_print
                tool_report_print("Running tool:", f"{function_name}({function_args_str})")
                
                # Get function signature
                sig = inspect.signature(function_to_call)
                
                # Convert arguments to appropriate types
                converted_args = function_args.copy()
                for param_name, param in sig.parameters.items():
                    if param_name in converted_args and hasattr(assistant, 'convert_to_pydantic_model'):
                        converted_args[param_name] = assistant.convert_to_pydantic_model(
                            param.annotation, converted_args[param_name]
                        )
                
                # Execute the function with converted arguments
                function_response = function_to_call(**converted_args)
                
                # Signal tool execution completion for real-time UI updates
                print(f"Tool execution completed: {function_name}")
                tool_report_print("Result:", str(function_response))
                
                # Find and update the tool call object with the result
                for tc in assistant.current_tool_calls:
                    if tc["id"] == tool_id:
                        tc["status"] = "completed"
                        tc["result"] = str(function_response)
                        # Send update via callback
                        if tool_event_callback:
                            for chunk in tool_event_callback("tool_update", tc):
                                yield chunk
                        break
                
                # Report tool execution results
                if print_response:
                    print(f"{Fore.CYAN}Tool call: {function_name}({function_args}) => {function_response}{Style.RESET_ALL}")
                
                # Add tool call result to conversation
                assistant.add_toolcall_output(tool_id, function_name, function_response)
                
            except Exception as tool_execution_error:
                # Handle any errors from the tool execution itself
                err_msg = f"Error executing tool {function_name}: {tool_execution_error}"
                print(f"{Fore.RED}{err_msg}{Style.RESET_ALL}")
                tool_report_print("Result:", f"Error: {str(tool_execution_error)}", is_error=True)
                
                # Update tool call with error status
                for tc in assistant.current_tool_calls:
                    if tc["id"] == tool_id:
                        tc["status"] = "error"
                        tc["result"] = f"Error: {str(tool_execution_error)}"
                        # Send update via callback
                        if tool_event_callback:
                            for chunk in tool_event_callback("tool_update", tc):
                                yield chunk
                        break
                
                assistant.add_toolcall_output(tool_id, function_name, f"Error: {str(tool_execution_error)}")
                has_errors = True
            
        except json.JSONDecodeError as e:
            # Handle JSON parsing errors
            err_msg = f"Failed to decode tool arguments for {function_name}: {e}. Arguments: {function_args_str}"
            print(f"{Fore.RED}{err_msg}{Style.RESET_ALL}")
            
            # Update tool call with error status
            for tc in assistant.current_tool_calls:
                if tc["id"] == tool_id:
                    tc["status"] = "error"
                    tc["result"] = err_msg
                    # Send update via callback
                    if tool_event_callback:
                        for chunk in tool_event_callback("tool_update", tc):
                            yield chunk
                    break
            
            assistant.add_toolcall_output(tool_id, function_name, err_msg)
            has_errors = True
        except Exception as e:
            # Handle any other errors during execution
            err_msg = f"Error executing tool {function_name}: {e}"
            print(f"{Fore.RED}{err_msg}{Style.RESET_ALL}")
            
            # Update tool call with error status
            for tc in assistant.current_tool_calls:
                if tc["id"] == tool_id:
                    tc["status"] = "error"
                    tc["result"] = err_msg
                    # Send update via callback
                    if tool_event_callback:
                        for chunk in tool_event_callback("tool_update", tc):
                            yield chunk
                    break
            
            assistant.add_toolcall_output(tool_id, function_name, err_msg)
            has_errors = True

    # Get the next response after processing all tool calls
    try:
        # Send temporary info message if callback is provided - commented out to not send to UI
        # if tool_event_callback:
        #    for chunk in tool_event_callback("info", "Getting next response after tool execution...", True):
        #        yield chunk
        
        # Just log to console instead
        print("Getting next response after tool execution...")
                
        # Get next response based on provider
        if assistant.provider == 'pollinations':
            if not assistant.api_client:
                raise ValueError("Pollinations provider selected but api_client is not initialized.")
            
            # Preprocess messages before passing to Pollinations API
            from assistant.api_client import preprocess_messages_for_pollinations
            
            try:
                next_response = assistant.api_client.get_completion(
                    messages=assistant.messages,
                    tools=assistant.tools
                )
            except Exception as e:
                print(f"{Fore.RED}Error making API call to Pollinations: {e}{Style.RESET_ALL}")
                # Create a minimal error response
                error_message = f"Error: {str(e)}"
                next_response = {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": f"I apologize, but I encountered an error while processing your request: {error_message}"
                        }
                    }]
                }
        elif assistant.provider == 'litellm':
            # Use litellm for completion without streaming (for tool processing)
            import litellm
            import config as conf
            
            # Debug log the messages to understand the current conversation state
            print(f"Messages before follow-up call (count: {len(assistant.messages)}):")
            for i, msg in enumerate(assistant.messages[-3:] if len(assistant.messages) > 3 else assistant.messages):
                # Add safer content handling to prevent None subscript error
                content = msg.get('content', '')
                content_preview = str(content)[:50] if content is not None else "None"
                print(f"  Message {i}: role={msg.get('role')}, content={content_preview}...")
                
                if msg.get('role') == 'tool' and 'tool_call_id' in msg:
                    # Also handle potential None content in tool results
                    tool_content = msg.get('content', '')
                    tool_preview = str(tool_content)[:50] if tool_content is not None else "None" 
                    print(f"    Tool result for call {msg.get('tool_call_id')}: {tool_preview}...")

            # Process messages to ensure proper format for Gemini vision models
            processed_messages = preprocess_messages_for_litellm(assistant.messages, assistant.model)
            
            completion_args = {
                "model": assistant.model,
                "messages": processed_messages,  # Use processed messages
                "tools": assistant.tools,
                "temperature": conf.TEMPERATURE,
                "top_p": conf.TOP_P,
                "max_tokens": conf.MAX_TOKENS,
                "seed": conf.SEED,
                "tool_choice": "auto",  # This is important to enable tool selection
            }
            safety_settings = getattr(conf, 'SAFETY_SETTINGS', None)
            if safety_settings:
                completion_args["safety_settings"] = safety_settings
                
            completion_args = {k: v for k, v in completion_args.items() if v is not None}
            
            print(f"Making follow-up LiteLLM API call with model: {assistant.model}")
            try:
                next_response = litellm.completion(**completion_args)
                print(f"Follow-up API call successful. Response type: {type(next_response)}")
                
                # Debug the response content - make this more robust with defensive checks
                if isinstance(next_response, dict) and "choices" in next_response and next_response["choices"]:
                    message = next_response["choices"][0].get("message", {})
                    content = message.get("content", "")
                    tool_calls = message.get("tool_calls")
                    print(f"Follow-up response content: {content[:100]}...")
                    print(f"Follow-up response has {len(tool_calls) if tool_calls else 0} tool calls")
                else:
                    # Handle different response formats from different LiteLLM backend providers
                    # For some providers, the response might be a custom object, not a dict
                    try:
                        # Check if response has a structured format we can navigate
                        if hasattr(next_response, 'choices') and next_response.choices:
                            # Try to get content from the structured response
                            message = getattr(next_response.choices[0], 'message', None)
                            if message:
                                content = getattr(message, 'content', '')
                                tool_calls = getattr(message, 'tool_calls', [])
                                print(f"Follow-up response (object format) content: {content[:100] if content else ''}...")
                                print(f"Follow-up response has {len(tool_calls) if tool_calls else 0} tool calls")
                            else:
                                print("Follow-up response message attribute is not accessible")
                        else:
                            print(f"Follow-up response has non-standard format: {type(next_response)}")
                    except Exception as format_error:
                        print(f"Error parsing response format: {format_error}")
                    
                    print(f"WARNING: Follow-up response format: {type(next_response)}")
                    # Try to dump some useful info about the response for debugging
                    try:
                        if hasattr(next_response, '__dict__'):
                            print(f"Response attributes: {list(next_response.__dict__.keys())}")
                        else:
                            print(f"Response repr: {repr(next_response)[:200]}...")
                    except:
                        print("Could not extract additional response details")
            except Exception as e:
                print(f"Error during follow-up LiteLLM API call: {e}")
                raise
        else:
            raise ValueError(f"Unsupported provider: {assistant.provider}")
        
        # Process the new response recursively, incrementing the recursion depth
        next_generator = process_tool_calls(
            assistant,
            next_response, 
            print_response=print_response, 
            validation_retries=2,
            recursion_depth=recursion_depth+1,
            tool_event_callback=tool_event_callback
        )
        
        # Pass through all events from the recursive call
        final_text = None
        for event in next_generator:
            if isinstance(event, dict) and "final_text" in event:
                final_text = event["final_text"]
            else:
                yield event
                
        # Yield the final text as a special event
        if final_text is not None:
            yield {"final_text": final_text}
            
            # Only yield token events once at the last recursion level, not for every level
            # This prevents duplicated final responses
            if tool_event_callback and recursion_depth == 0:
                for chunk in tool_event_callback("token", final_text):
                    yield chunk
            
        # If we didn't get a final text but have content from the follow-up response, create one
        elif hasattr(next_response, 'choices') and next_response.choices and recursion_depth > 0:
            # Try to extract content from object-style responses
            try:
                message = getattr(next_response.choices[0], 'message', None)
                if message and hasattr(message, 'content') and message.content:
                    print(f"Extracted content from follow-up response: {message.content[:100]}...")
                    final_text = message.content
                    yield {"final_text": final_text}
                    
                    # Only yield token events once at the last recursion level
                    if tool_event_callback and recursion_depth == 0:
                        for chunk in tool_event_callback("token", final_text):
                            yield chunk
                    
                    # Also add to assistant messages if it's not already there
                    if message.content and not any(m.get('content') == message.content for m in assistant.messages[-3:] if m.get('role') == 'assistant'):
                        assistant.messages.append({
                            "role": "assistant",
                            "content": message.content
                        })
                        assistant._final_response = message.content
            except Exception as e:
                print(f"Error extracting content from follow-up response: {e}")
                # If extraction fails but we have a tool result, use it as a fallback
                if assistant.messages and assistant.messages[-1].get('role') == 'tool':
                    tool_content = assistant.messages[-1].get('content', '')
                    fallback = f"I found that the current time is {tool_content}."
                    yield {"final_text": fallback}
                    
                    # Only yield token events once at the last recursion level
                    if tool_event_callback and recursion_depth == 0:
                        for chunk in tool_event_callback("token", fallback):
                            yield chunk
    except Exception as e:
        print(f"{Fore.RED}Error in recursive tool call: {e}. Returning partial results.{Style.RESET_ALL}")
        import traceback
        traceback.print_exc()
        # Return partial results if we encounter an error in the recursive call
        fallback = "Error processing follow-up response. Here's what I know so far."
        yield {"final_text": fallback}
        
        # Only yield token events once at the last recursion level
        if tool_event_callback and recursion_depth == 0:
            for chunk in tool_event_callback("token", fallback):
                yield chunk

def convert_to_pydantic_model(annotation, arg_value):
    """
    Attempts to convert a value to a Pydantic model.
    
    Args:
        annotation: Type annotation
        arg_value: Value to convert
        
    Returns:
        Converted value
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        try:
            return annotation(**arg_value)
        except (TypeError, ValueError):
            return arg_value
    elif hasattr(annotation, "__origin__"):
        origin = annotation.__origin__
        args = annotation.__args__

        if origin is list:
            return [
                convert_to_pydantic_model(args[0], item) for item in arg_value
            ]
        elif origin is dict:
            return {
                key: convert_to_pydantic_model(args[1], value)
                for key, value in arg_value.items()
            }
        elif origin is Union:
            for arg_type in args:
                try:
                    return convert_to_pydantic_model(arg_type, arg_value)
                except (ValueError, TypeError):
                    continue
            raise ValueError(f"Could not convert {arg_value} to any type in {args}")
        elif origin is tuple:
            return tuple(
                convert_to_pydantic_model(args[i], arg_value[i])
                for i in range(len(args))
            )
        elif origin is set:
            return {
                convert_to_pydantic_model(args[0], item) for item in arg_value
            }
    return arg_value
