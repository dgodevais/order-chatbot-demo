import os
import time
from typing import Dict, Any, List, Optional, Union, ClassVar, Type
import logging
from llama_index.core import Settings
from llama_index.core.workflow import Workflow, Context, Event, step, StopEvent, StartEvent
from llama_index.llms.openai import OpenAI
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.core.base.llms.types import MessageRole, ChatMessage
from utils import menu_to_string
import json

# Set up logger
logger = logging.getLogger("food_ordering_bot")

# Define custom event classes
class ResponseEvent(Event):
    """Event containing the response content and action type."""
    response: str
    action_type: str
    original_query: Optional[str] = None # Add field to carry original query
    cart_items: Optional[List[Dict[str, Any]]] = None # Add field for cart items
    cart_status: Optional[str] = None # Add field for cart status
    prompt_tokens: Optional[int] = None # Add field for prompt tokens
    completion_tokens: Optional[int] = None # Add field for completion tokens

class ChatResponseStopEvent(StopEvent):
    """Custom StopEvent with response and action_type fields."""
    response: str
    action_type: str
    cart_items: Optional[List[Dict[str, Any]]] = None # Add field for cart items
    cart_status: Optional[str] = None # Add field for cart status
    prompt_tokens: Optional[int] = None # Add field for prompt tokens
    completion_tokens: Optional[int] = None # Add field for completion tokens

class FoodOrderingWorkflow(Workflow):
    """
    A workflow that implements the food ordering chatbot conversation logic.
    """
    # Define the start event class
    start_event_cls: ClassVar[Type[Event]] = StartEvent
    
    def __init__(self, menu: Dict[str, Dict[str, Any]], chat_history: List[ChatMessage] = None, timeout: float = 60.0):
        # Configure LLM settings - do this before calling super().__init__()
        Settings.llm = OpenAI(model="gpt-4o", request_timeout=30)
        Settings.embed_model = OpenAIEmbedding(model="text-embedding-ada-002")
        
        # Call super().__init__() with explicit timeout
        super().__init__(timeout=timeout)
        
        # Set instance attributes
        self.menu = menu
        self.chat_history = chat_history or []
        self.menu_text = menu_to_string(menu)
        # Add token tracking variables
        self.last_prompt_tokens = None
        self.last_completion_tokens = None
        logger.info(f"FoodOrderingWorkflow initialized with timeout={timeout}")
    
    async def _format_response_text(self, text: str) -> str:
        """
        Uses an LLM to format the text response for consistent, plain text output,
        ensuring lists are lettered (A, B, C...) and suitable for SMS/basic chat.
        Removes extraneous markdown.
        """
        logger.info(f"Formatting text (input): {text[:100]}...") # Log input

        # Handle empty or whitespace-only input gracefully
        if not text or text.isspace():
            logger.info("Formatting text (input was empty/whitespace): Returning empty string.")
            return ""

        format_prompt = f"""
        You are a text formatting assistant. Your task is to take the input text, clean it up, and format it for display in a simple text-based chat interface (like SMS).

        **Formatting Rules (Apply these STRICTLY):**
        1.  **Plain Text:** The final output MUST be plain text only. Remove ALL markdown formatting (like **, *, _, etc.). Exception: Keep existing newline characters (`\\n`) where they make sense for readability.
        2.  **List Formatting:** Identify any lists of choices or options presented to the user. Reformat these lists so EACH item starts with a capital letter followed by a period and a space (A., B., C., etc.). Each item MUST be on its own line. Make sure the original item text follows the letter label. Preserve any existing list structure if it already uses letter labels correctly, just clean markdown.
        3.  **Clarity:** Ensure the text is clear and easy to read. Do not add any conversational text, greetings, questions, or suggestions that were not present in the original input. Your ONLY job is to clean and reformat the *existing* text according to the rules.
        4.  **No Extra Content:** Do not add headers, footers, or any text not derived from the original input.
        5.  **Whitespace:** Trim leading/trailing whitespace from the final output. Preserve internal newlines essential for structure (like between list items or paragraphs). Ensure consistent line breaks around list items.

        **Example Input 1 (with markdown and bad list):**
        "Okay, we have the *Classic Chicken Sandwich* ($8.99) and the **Spicy Deluxe** ($9.99). Would you like one?\\n- Option 1: Classic\\n- Option 2: Spicy"

        **Example Output 1 (formatted):**
        "Okay, we have the Classic Chicken Sandwich ($8.99) and the Spicy Deluxe ($9.99). Would you like one?
        A. Classic
        B. Spicy"

        **Example Input 2 (already somewhat formatted but needs standardization):**
        "We have these pizza options:\\nA. Pepperoni Pizza ($12.00)\\nB. Margherita Pizza ($11.00)\\nWould you like to add one?"

        **Example Output 2 (standardized):**
        "We have these pizza options:
        A. Pepperoni Pizza ($12.00)
        B. Margherita Pizza ($11.00)
        Would you like to add one?"

        **Example Input 3 (Confirmation prompt):**
        "Your order:\\n- 1 Classic Chicken Sandwich (extra cheese): $9.99\\n\\nTotal: $9.99\\n\\nWould you like to confirm this order?\\nA. Yes, confirm my order\\nB. No, I'd like to make changes"

        **Example Output 3 (already correct, should pass through cleaned):**
        "Your order:
        - 1 Classic Chicken Sandwich (extra cheese): $9.99

        Total: $9.99

        Would you like to confirm this order?
        A. Yes, confirm my order
        B. No, I'd like to make changes"

        **Input Text to Format:**
        ---
        {text}
        ---

        **Formatted Output:**
        """

        llm = OpenAI(model="gpt-4o", temperature=0.0, request_timeout=20) # Use a capable model, maybe shorter timeout ok
        try:
            response = await llm.acomplete(format_prompt)
            formatted_text = response.text.strip()
            logger.info(f"Formatting text (output): {formatted_text[:100]}...") # Log output

            # Basic validation: Check if the LLM returned an empty string when the input wasn't empty
            if not formatted_text and text and not text.isspace():
                logger.warning("Formatter LLM returned empty string unexpectedly. Falling back to original text.")
                return text # Fallback

            # --- START: Store Token Usage (Optional but good practice) ---
            prompt_tokens = None
            completion_tokens = None
            try:
                if hasattr(response, 'additional_kwargs') and isinstance(response.additional_kwargs, dict):
                    kwargs_dict = response.additional_kwargs
                    if 'prompt_tokens' in kwargs_dict:
                        p_tokens = kwargs_dict['prompt_tokens']
                        prompt_tokens = int(p_tokens) if isinstance(p_tokens, (int, str)) and str(p_tokens).isdigit() else None
                    if 'completion_tokens' in kwargs_dict:
                        c_tokens = kwargs_dict['completion_tokens']
                        completion_tokens = int(c_tokens) if isinstance(c_tokens, (int, str)) and str(c_tokens).isdigit() else None
                logger.debug(f"Formatter Tokens: p={prompt_tokens}, c={completion_tokens}")
            except Exception as token_err:
                logger.error(f"Error extracting formatter token counts: {token_err}")
            # (Note: We aren't currently *using* these tokens elsewhere, but it's good to extract)
            # --- END: Store Token Usage ---

            return formatted_text
        except Exception as e:
            logger.error(f"Error during text formatting LLM call: {type(e).__name__}: {str(e)}. Returning original text.")
            return text # Fallback to original text on error

    @step
    async def classify_and_respond(self, ctx: Context, ev: StartEvent) -> Union[ResponseEvent, ChatResponseStopEvent]:
        """
        First step: Classify intent. 
        If menu/order, return ResponseEvent with pending action type.
        If greeting/end/irrelevant, return ChatResponseStopEvent directly to end workflow.
        """
        # Get the query from the start event
        query = ev.content # Correct way to access StartEvent content
        logger.info(f"Processing query: '{query}'")

        # Format chat history for the prompt
        formatted_history = "\n".join([
            f"{'USER' if msg.role == MessageRole.USER else 'ASSISTANT'}: {msg.content}"
            for msg in self.chat_history
        ]) if self.chat_history else "No conversation history yet."

        # Determine intent with prompt, now including history
        router_prompt = f"""
        You are a restaurant chatbot assistant classifying user intent.
        
        **ABSOLUTE RULES - APPLY THESE FIRST:**
        1.  If the user message is a simple, standalone greeting (e.g., "hello", "hi"), the intent is GREETING.
        2.  If the user message is asking *about the conversation itself* (e.g., "what did I ask?", "what was my last message?", "what did we talk about?"), the intent is HISTORY, regardless of the topic of previous messages.
        3.  If the user indicates they are done ordering with phrases like "nothing else", "that's all", "I'm done", "that's it", or similar, the intent is ORDER_CONFIRMATION (not END).

        **Conversation History:**
        {formatted_history}
        
        **Current User Message:** "{query}"
        
        Given the user message, conversation history, and the absolute rules, classify the intent into ONE of the following:
        - GREETING: A simple greeting. **Must follow Absolute Rule 1.**
        - HISTORY: A question *about* the conversation history or previous messages/orders. **Must follow Absolute Rule 2.**
        - MENU: Asking about menu items/prices/descriptions. *Excludes questions about what was previously discussed.*
        - ORDER: Requesting to create, modify, review, or cancel an order. *Excludes questions about previous orders already discussed.*
        - ORDER_CONFIRMATION: Confirming a pending order (e.g., "confirm my order", "yes I want to order", "place my order", "I confirm", "that's correct") or indicating they are done ordering (e.g., "nothing else", "that's all", "I'm done"). **Must follow Absolute Rule 3.**
        - END: Ending the conversation (e.g., "bye", "thank you") without intent to complete an order. This is ONLY for final goodbyes, not for completing an order.
        - IRRELEVANT: Any other topic not covered above.
        
        Output Instructions:
        1. Return STRICTLY a JSON object with keys "intent" and "response".
        2. For GREETING or HISTORY intents: Set the correct "intent". Provide a **concise**, **plain text** response (under 320 characters, no markdown) in the "response" field. Use the provided Conversation History context to answer HISTORY questions accurately. Summarize if necessary and offer to provide more detail if relevant.
        3. For IRRELEVANT intent: Set "intent" to "IRRELEVANT". Provide a **concise**, empathetic **plain text** refusal/explanation (under 320 characters, no markdown) in the "response" field.
        4. For MENU, ORDER, ORDER_CONFIRMATION, or END intents: Set the correct "intent" and set "response" to an empty string ("").
        
        Examples (History examples assume relevant context was in the provided history):
        User message: "hello"
        Output: {{"intent": "GREETING", "response": "Hello! How can I help with the menu or your order?"}} # Plain text

        User message: "What drinks do you have?"
        Output: {{"intent": "MENU", "response": ""}}

        User message: "I'd like to confirm my order"
        Output: {{"intent": "ORDER_CONFIRMATION", "response": ""}}

        User message: "Yes I want to place this order"
        Output: {{"intent": "ORDER_CONFIRMATION", "response": ""}}
        
        User message: "nothing else"
        Output: {{"intent": "ORDER_CONFIRMATION", "response": ""}}
        
        User message: "that's all"
        Output: {{"intent": "ORDER_CONFIRMATION", "response": ""}}
        
        User message: "I'm done"
        Output: {{"intent": "ORDER_CONFIRMATION", "response": ""}}

        User message: "what did I ask before this?"
        Output: {{"intent": "HISTORY", "response": "You previously asked about our sandwich options. Need more details on those, or can I help with something else?"}} # Plain text

        User message: "What was my previous message?"
        Output: {{"intent": "HISTORY", "response": "Your previous message was asking about drinks. Anything else I can help with?"}} # Plain text

        User message: "what was the first thing I asked?"
        Output: {{"intent": "HISTORY", "response": "Looks like your first message was 'Hello'. How can I help now?"}} # Plain text

        User message: "what did I order last time?"
        Output: {{"intent": "HISTORY", "response": "We discussed you ordering pizza previously. Want to order that now or see the menu again?"}} # Plain text

        User message: "tell me a joke"
        Output: {{"intent": "IRRELEVANT", "response": "Sorry, I can't tell jokes! I'm here for menu questions or orders. Can I help with that?"}} # Plain text

        User message: "thanks bye"
        Output: {{"intent": "END", "response": ""}}

        Ensure no extra text before or after the JSON object.
        """

        llm = OpenAI(model="gpt-4o", temperature=0.0, request_timeout=30)
        logger.info("Sending intent classification request to OpenAI")
        
        # Measure response time for intent classification
        start_time = time.time()
        router_response = await llm.acomplete(router_prompt)
        elapsed = time.time() - start_time
        
        # Detailed debug logging of the raw response object
        response_type = type(router_response).__name__
        has_additional_kwargs = hasattr(router_response, 'additional_kwargs')
        additional_kwargs_type = type(getattr(router_response, 'additional_kwargs', None)).__name__
        logger.info(f"DEBUG: Response type={response_type}, has_additional_kwargs={has_additional_kwargs}, additional_kwargs_type={additional_kwargs_type}")
        
        if has_additional_kwargs:
            additional_kwargs = router_response.additional_kwargs
            logger.info(f"DEBUG: additional_kwargs keys: {additional_kwargs.keys() if isinstance(additional_kwargs, dict) else 'Not a dict'}")
            if isinstance(additional_kwargs, dict) and 'token_usage' in additional_kwargs:
                token_usage = additional_kwargs['token_usage']
                logger.info(f"DEBUG: token_usage={token_usage}, type={type(token_usage)}")
        
        # Extract token usage with extensive error handling (Corrected)
        prompt_tokens = None
        completion_tokens = None
        try:
            if hasattr(router_response, 'additional_kwargs') and isinstance(router_response.additional_kwargs, dict):
                kwargs_dict = router_response.additional_kwargs # Get the dictionary directly
                logger.debug(f"Token Extraction: kwargs_dict = {kwargs_dict}") # Log the dict
                if 'prompt_tokens' in kwargs_dict:
                    p_tokens = kwargs_dict['prompt_tokens'] # Read the value
                    logger.debug(f"Token Extraction: Found p_tokens = {p_tokens} (type: {type(p_tokens)})")
                    if isinstance(p_tokens, str) and p_tokens.isdigit():
                        prompt_tokens = int(p_tokens)
                        logger.debug(f"Token Extraction: Assigned prompt_tokens = {prompt_tokens} (from str)")
                    elif isinstance(p_tokens, int):
                        prompt_tokens = p_tokens # Assign if already int
                        logger.debug(f"Token Extraction: Assigned prompt_tokens = {prompt_tokens} (from int)")
                    else:
                        logger.debug("Token Extraction: p_tokens was not str/digit or int.")
                else:
                    logger.debug("Token Extraction: 'prompt_tokens' key not found.")

                if 'completion_tokens' in kwargs_dict:
                    c_tokens = kwargs_dict['completion_tokens'] # Read the value
                    logger.debug(f"Token Extraction: Found c_tokens = {c_tokens} (type: {type(c_tokens)})")
                    if isinstance(c_tokens, str) and c_tokens.isdigit():
                        completion_tokens = int(c_tokens)
                        logger.debug(f"Token Extraction: Assigned completion_tokens = {completion_tokens} (from str)")
                    elif isinstance(c_tokens, int):
                        completion_tokens = c_tokens # Assign if already int
                        logger.debug(f"Token Extraction: Assigned completion_tokens = {completion_tokens} (from int)")
                    else:
                        logger.debug("Token Extraction: c_tokens was not str/digit or int.")
                else:
                     logger.debug("Token Extraction: 'completion_tokens' key not found.")
            else:
                logger.debug("Token Extraction: No valid additional_kwargs found.")
        except Exception as token_err:
            logger.error(f"Error extracting token counts: {token_err}")
            # Ensure they remain None on error
            prompt_tokens = None
            completion_tokens = None
            
        # Store token info in class variables for other methods to access
        self.last_prompt_tokens = prompt_tokens
        self.last_completion_tokens = completion_tokens
        
        # Parse the JSON response
        response_text = router_response.text.strip()
        # Updated log message
        logger.info(f"Router response (took {elapsed:.2f}s, prompt_tokens={prompt_tokens}, completion_tokens={completion_tokens}): {response_text}")
        
        # Extract intent and direct response from JSON
        intent = ""
        direct_response = "" # Renamed from general_response for clarity
        
        try:
            # Parse the JSON response
            response_data = json.loads(response_text)
            
            # Extract the fields with better validation
            if isinstance(response_data, dict):
                intent = response_data.get("intent", "").lower() if response_data.get("intent") else ""
                direct_response = response_data.get("response", "") if response_data.get("response") else ""
                
                # Validate the extracted data
                valid_intents = ["menu", "order", "greeting", "end", "irrelevant", "history", "order_confirmation"]
                if not intent or intent not in valid_intents:
                    # Default to irrelevant if intent is invalid or missing
                    logger.warning(f"Invalid or missing intent '{intent}', defaulting to 'irrelevant'")
                    intent = "irrelevant"
                    # Ensure we provide a default refusal if the intent was bad AND no response was given
                    if not direct_response:
                         direct_response = "I'm sorry, I encountered an issue. I can only assist with menu questions and food orders." # Plain text

                # If intent is GREETING, IRRELEVANT, or HISTORY, check response validity
                if intent in ["greeting", "irrelevant", "history"]:
                   if not isinstance(direct_response, str) or not direct_response.strip():
                       logger.warning(f"Invalid or empty direct response for intent '{intent}': '{direct_response}'. Using fallback message.")
                       # Provide a default response based on the (potentially defaulted) intent
                       if intent == "greeting":
                            direct_response = "Hello! How can I help with the menu or your order?" # Plain text
                       elif intent == "history":
                            direct_response = "I can see you're asking about our previous conversation. How can I help you with our menu or placing an order?" # Plain text
                       else: # irrelevant
                            direct_response = "I'm sorry, I can only assist with questions about our menu and help you place an order." # Plain text
                   elif direct_response.strip() in ['{', '}', '[]', '[', ']', '{}', ':', '""', "''", ',', '.']:
                       logger.warning(f"Direct response for intent '{intent}' looks like a fragment: '{direct_response}'. Using fallback message.")
                       if intent == "greeting":
                            direct_response = "Hello! How can I help with the menu or your order?" # Plain text
                       elif intent == "history":
                            direct_response = "I can see you're asking about our previous conversation. How can I help you with our menu or placing an order?" # Plain text
                       else: # irrelevant
                            direct_response = "I'm sorry, I can only assist with questions about our menu and help you place an order." # Plain text
                
                logger.info(f"Successfully parsed JSON response: intent='{intent}', response_length={len(direct_response)}")
            else:
                logger.error("Response data is not a dictionary, defaulting to irrelevant")
                intent = "irrelevant"
                direct_response = "I'm sorry, I encountered an issue processing the response. I can only assist with menu questions and food orders." # Plain text
        except json.JSONDecodeError as e:
            # Fallback if JSON parsing fails
            logger.error(f"Failed to parse JSON response: {e}")
            logger.info(f"Raw response text was: {response_text}")
            logger.info("Attempting keyword-based intent extraction as fallback...")
            
            # Simple keyword check on the raw text
            raw_text_lower = response_text.lower()
            if "menu" in raw_text_lower:
                intent = "menu"
                logger.info("Fallback: Detected 'menu' keyword.")
            elif "order" in raw_text_lower or "cart" in raw_text_lower or "checkout" in raw_text_lower:
                intent = "order"
                logger.info("Fallback: Detected 'order/cart/checkout' keyword.")
            elif any(greeting in raw_text_lower for greeting in ["hello", "hi ", " how are"]):
                 intent = "greeting"
                 direct_response = "Hello! How can I help you with the menu or your order today?" # Provide default greeting - Plain text
                 logger.info("Fallback: Detected greeting keyword.")
            elif any(farewell in raw_text_lower for farewell in ["bye", "thank you", "thanks"]):
                 intent = "end"
                 logger.info("Fallback: Detected farewell keyword.")
            else:
                # Only default to irrelevant if no keywords match
                intent = "irrelevant"
                direct_response = "I'm sorry, I had trouble understanding that. I can only assist with menu questions and food orders." # Specific message for this fallback path - Plain text
                logger.info("Fallback: No relevant keywords detected, defaulting to irrelevant.")
                
        logger.info(f"Intent classified as: '{intent}' (took {elapsed:.2f}s)")
        
        # Generate appropriate response or acknowledgment based on intent
        response = ""
        action_type = ""
        result = None

        try:
            if intent == "menu":
                logger.info("Intent: MENU. Returning acknowledgment.")
                response = "Give us a moment while we research that for you." # Plain text ack
                action_type = "menu_inquiry_pending" # Temporary type
                result = ResponseEvent(
                    response=response,
                    action_type=action_type,
                    original_query=query, # Pass query for next step
                    prompt_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
                    completion_tokens=completion_tokens if isinstance(completion_tokens, int) else None
                )
            elif intent == "order":
                logger.info("Intent: ORDER. Returning acknowledgment.")
                # Basic check for modification keywords - can be enhanced
                if any(kw in query.lower() for kw in ["change", "modify", "add", "remove", "update"]):
                     response = "Give us a moment while we get that order modification ready for you." # Plain text ack
                else:
                     response = "Give us a moment while we get that order ready for you." # Plain text ack
                action_type = "order_action_pending" # Temporary type
                result = ResponseEvent(
                    response=response,
                    action_type=action_type,
                    original_query=query, # Pass query for next step
                    prompt_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
                    completion_tokens=completion_tokens if isinstance(completion_tokens, int) else None
                )
            elif intent == "order_confirmation":
                logger.info("Intent: ORDER_CONFIRMATION. Handling order confirmation.")
                response = "Confirming your order..." # Plain text ack
                action_type = "order_confirmation_pending"
                result = ResponseEvent(
                    response=response,
                    action_type=action_type,
                    original_query=query,
                    prompt_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
                    completion_tokens=completion_tokens if isinstance(completion_tokens, int) else None
                )
            elif intent == "greeting":
                logger.info("Handling greeting directly")
                # Use the direct response (already plain text)
                response = direct_response
                action_type = "greeting"
                # Log tokens right before creating the event
                logger.info(f"DEBUG (classify_and_respond): Tokens before creating greeting StopEvent: prompt={prompt_tokens}, completion={completion_tokens}")
                # Return a ChatResponseStopEvent directly for greeting
                result = ChatResponseStopEvent(
                    result=None,  # Required by StopEvent
                    response=response,
                    action_type=action_type,
                    prompt_tokens=prompt_tokens, # Pass tokens directly
                    completion_tokens=completion_tokens # Pass tokens directly
                )
            elif intent == "end":
                logger.info("Handling end conversation")
                # Call _handle_end_conversation directly as it's simple
                raw_response = await self._handle_end_conversation(query) # Gets plain text
                response = raw_response # Use plain text directly
                action_type = "end_conversation"
                # Return a ChatResponseStopEvent directly for end
                result = ChatResponseStopEvent(
                    result=None,  # Required by StopEvent
                    response=response,
                    action_type=action_type,
                    prompt_tokens=self.last_prompt_tokens, # Use tokens from the handler
                    completion_tokens=self.last_completion_tokens # Use tokens from the handler
                )
            elif intent == "history":
                logger.info("Handling history query directly")
                # Use the direct response (already plain text)
                response = direct_response
                action_type = "history_query"
                # Return a ChatResponseStopEvent directly for history
                result = ChatResponseStopEvent(
                    result=None,  # Required by StopEvent
                    response=response,
                    action_type=action_type,
                    prompt_tokens=prompt_tokens, # Pass tokens directly
                    completion_tokens=completion_tokens # Pass tokens directly
                 )
            elif intent == "irrelevant":
                 logger.info("Handling irrelevant query directly")
                 # Use the direct response (already plain text)
                 response = direct_response
                 action_type = "irrelevant_query"
                 # Return a ChatResponseStopEvent directly for irrelevant
                 result = ChatResponseStopEvent(
                    result=None,  # Required by StopEvent
                    response=response,
                    action_type=action_type,
                    prompt_tokens=prompt_tokens, # Pass tokens directly
                    completion_tokens=completion_tokens # Pass tokens directly
                 )
            else: # Should not happen due to validation, but good to have a fallback
                logger.error(f"Reached unexpected else block for intent: {intent}")
                response = "I'm sorry, I'm not sure how to handle that. I can assist with menu questions and orders." # Plain text error
                action_type = "error"
                # Return a ChatResponseStopEvent directly for error
                result = ChatResponseStopEvent(
                    result=None,  # Required by StopEvent
                    response=response,
                    action_type=action_type,
                    prompt_tokens=prompt_tokens, # Pass tokens directly
                    completion_tokens=completion_tokens # Pass tokens directly
                )

            logger.info(f"Step 1 Result: Type='{action_type}', Response='{response[:50]}...', Prompt Tokens={result.prompt_tokens if hasattr(result, 'prompt_tokens') else 'N/A'}, Completion Tokens={result.completion_tokens if hasattr(result, 'completion_tokens') else 'N/A'}")
            return result
            
        except Exception as e:
            logger.error(f"Error in classify_and_respond logic block: {type(e).__name__}: {str(e)}")
            # Provide a generic refusal in case of errors in handlers
            # Ensure formatting is done here if needed, or keep it simple
            simple_error_response = "I'm sorry, I encountered an error and cannot process your request. I can only assist with menu questions and food orders." # Plain text error
            return ChatResponseStopEvent(
                result=None,  # Required by StopEvent
                response=simple_error_response, # Use simple text
                action_type="error",
                prompt_tokens=None,
                completion_tokens=None
            )
    
    @step
    async def generate_detailed_response(self, ctx: Context, ev: ResponseEvent) -> ResponseEvent:
        """
        Second step: If the previous step returned a pending action type,
        generate the detailed response using the appropriate handler.
        Otherwise, pass the event through by creating a new event object.
        """
        logger.info(f"Entering generate_detailed_response with action_type: {ev.action_type}")
        
        # Initialize token variables to None for this step
        prompt_tokens = None
        completion_tokens = None
        
        if ev.action_type == "menu_inquiry_pending":
            logger.info("Handling pending menu inquiry")
            if ev.original_query:
                response_text = await self._handle_menu_query(ev.original_query) # Gets plain text
                # Format the response AFTER getting it
                formatted_response = await self._format_response_text(response_text)
                # Retrieve tokens stored by the handler
                prompt_tokens = self.last_prompt_tokens
                completion_tokens = self.last_completion_tokens
                logger.info(f"Generated detailed menu response: {formatted_response[:50]}...")
                return ResponseEvent(
                    response=formatted_response, # Use formatted text
                    action_type="menu_inquiry", # Final action type
                    cart_items=None, # No cart changes for menu inquiries
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens
                )
            else:
                logger.error("Original query missing for menu_inquiry_pending")
                return ResponseEvent(response="Error: Missing query for menu info.", action_type="error", cart_items=None, prompt_tokens=None, completion_tokens=None)
                
        elif ev.action_type == "order_action_pending":
            logger.info("Handling pending order action")
            if ev.original_query:
                response_text, cart_items = await self._handle_order_query(ev.original_query) # Gets plain text
                # Format the response AFTER getting it
                formatted_response = await self._format_response_text(response_text)
                 # Retrieve tokens stored by the handler
                prompt_tokens = self.last_prompt_tokens
                completion_tokens = self.last_completion_tokens
                logger.info(f"Generated detailed order response: {formatted_response[:50]}...")
                return ResponseEvent(
                    response=formatted_response, # Use formatted text
                    action_type="order_action", # Final action type
                    cart_items=cart_items, # Include cart items
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens
                )
            else:
                 logger.error("Original query missing for order_action_pending")
                 return ResponseEvent(response="Error: Missing query for order action.", action_type="error", cart_items=None, prompt_tokens=None, completion_tokens=None)
        
        elif ev.action_type == "order_confirmation_pending":
            logger.info("Handling pending order confirmation")
            if ev.original_query:
                response_text, cart_items, cart_status = await self._handle_order_confirmation(ev.original_query) # Gets plain text
                # Format the response AFTER getting it
                formatted_response = await self._format_response_text(response_text)
                # Retrieve tokens stored by the handler
                prompt_tokens = self.last_prompt_tokens
                completion_tokens = self.last_completion_tokens
                logger.info(f"Generated order confirmation response: {formatted_response[:50]}...")
                return ResponseEvent(
                    response=formatted_response, # Use formatted text
                    action_type="order_confirmation", # Final action type
                    cart_items=cart_items, # Include cart items
                    cart_status=cart_status, # Include updated cart status
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens
                )
            else:
                logger.error("Original query missing for order_confirmation_pending")
                return ResponseEvent(response="Error: Missing query for order confirmation.", action_type="error", cart_items=None, prompt_tokens=None, completion_tokens=None)
                 
        else:
            # If action type is already final (greeting, end, irrelevant, error, history), pass through
            # Explicitly create a new event object to avoid potential issues with object identity.
            logger.info(f"Passing through response with final action_type: {ev.action_type} by creating new event")
            # The response should already be plain text from classify_and_respond
            passthrough_response = ev.response 
            # Use the tokens from the incoming event, as no new handler was called
            return ResponseEvent(
                response=passthrough_response, # Use the original plain text response
                action_type=ev.action_type,
                original_query=ev.original_query, # Ensure all relevant fields are copied
                cart_items=ev.cart_items, # Pass through cart items
                cart_status=ev.cart_status, # Pass through cart status
                prompt_tokens=ev.prompt_tokens, # Pass through tokens from previous step
                completion_tokens=ev.completion_tokens # Pass through tokens from previous step
            )

    @step
    async def finalize(self, ctx: Context, ev: ResponseEvent) -> ChatResponseStopEvent:
        """
        Final step: Convert ResponseEvent to ChatResponseStopEvent
        """
        logger.info(f"Finalizing response: {ev.response[:30]}...")
        
        # The incoming ResponseEvent (ev) should now have the correct tokens
        # from either the initial classify_and_respond or the generate_detailed_response step.
        prompt_tokens = ev.prompt_tokens
        completion_tokens = ev.completion_tokens
        
        # Log token info being used in finalize
        logger.info(f"Finalize using token values: prompt_tokens={prompt_tokens}, completion_tokens={completion_tokens}")
        
        # Create our custom ChatResponseStopEvent with proper fields
        result = ChatResponseStopEvent(
            # Set 'result' to None (required by StopEvent) 
            result=None,
            # Add our custom fields
            response=ev.response,
            action_type=ev.action_type,
            cart_items=ev.cart_items, # Pass through cart items
            cart_status=ev.cart_status, # Pass through cart status
            prompt_tokens=prompt_tokens, # Use the tokens from the event
            completion_tokens=completion_tokens # Use the tokens from the event
        )
        logger.info(f"Created ChatResponseStopEvent with fields: response={result.response[:20]}..., action_type={result.action_type}, prompt_tokens={result.prompt_tokens}, completion_tokens={result.completion_tokens}")
        return result
    
    async def _handle_menu_query(self, query: str) -> str:
        """Handle menu-related queries"""
        menu_template = f"""
        You are a helpful restaurant assistant providing information about menu items and guiding users towards placing an order.
        **Use the provided conversation history to understand the context and avoid repeating information unnecessarily.**
        
        **Presenting Options:**
        - ALWAYS present menu items and options with letter labels (A, B, C, etc.) at the beginning of each choice.
        - When listing multiple items, present them in a clear lettered list format, with one option per line.
        - Example:
          We have these sandwich options:
          A. Classic Chicken Sandwich ($8.99)
          B. Spicy Deluxe Chicken Sandwich ($9.99)
          C. Grilled Chicken Club ($10.49)
        
        **Handling Follow-up for "More Details":**
        - If the user asks for "more details" after you've provided a summary, look at your *immediately preceding message* in the history.
        - Identify the items you summarized in that message.
        - Provide the *additional* details (like descriptions, options, ingredients) for *only those items*.
        - Do NOT repeat the item names and prices from the summary unless essential for context.
        
        **Guiding towards Purchase:**
        - After providing information about an item or category, gently ask if the user would like to add anything to their order or if they need more information.
        
        Be friendly and informative about prices and descriptions.
        Use standard text formatting. Use letter labels (A, B, C...) for options.
        Do NOT use any markdown (like ** for bolding).
        The complete menu is as follows:
        {self.menu_text}
        
        **Example Interaction (Presenting Options):**
        User: "What kind of pizzas do you have?"
        Assistant: "We have these pizza options:
        A. Pepperoni Pizza ($12.00)
        B. Margherita Pizza ($11.00)
        C. Veggie Pizza ($11.50)
        Would you like to add one to your order or hear more details about any of these options?"
        """
        
        # Generate response
        messages = self.chat_history + [
            ChatMessage(role=MessageRole.USER, content=query),
            ChatMessage(role=MessageRole.SYSTEM, content=menu_template)
        ]
        
        logger.info("_handle_menu_query: Sending request to OpenAI")
        llm = OpenAI(model="gpt-4o", temperature=0.0, request_timeout=30)
        try:
            # Measure response time for menu query
            start_time = time.time()
            response = await llm.achat(messages)
            elapsed = time.time() - start_time
            
            # Detailed debug logging of the raw response object
            response_type = type(response).__name__
            has_additional_kwargs = hasattr(response, 'additional_kwargs')
            additional_kwargs_type = type(getattr(response, 'additional_kwargs', None)).__name__
            logger.info(f"DEBUG: Response type={response_type}, has_additional_kwargs={has_additional_kwargs}, additional_kwargs_type={additional_kwargs_type}")
            
            if has_additional_kwargs:
                additional_kwargs = response.additional_kwargs
                logger.info(f"DEBUG: additional_kwargs keys: {additional_kwargs.keys() if isinstance(additional_kwargs, dict) else 'Not a dict'}")
                if isinstance(additional_kwargs, dict) and 'token_usage' in additional_kwargs:
                    token_usage = additional_kwargs['token_usage']
                    logger.info(f"DEBUG: token_usage={token_usage}, type={type(token_usage)}")
            
            # --- START: Robust Token Extraction (Corrected - Direct Access) ---
            prompt_tokens = None
            completion_tokens = None
            try:
                if hasattr(response, 'additional_kwargs') and isinstance(response.additional_kwargs, dict):
                    kwargs_dict = response.additional_kwargs # Get the dictionary directly
                    if 'prompt_tokens' in kwargs_dict:
                        p_tokens = kwargs_dict['prompt_tokens']
                        if isinstance(p_tokens, str) and p_tokens.isdigit():
                            prompt_tokens = int(p_tokens)
                        elif isinstance(p_tokens, int):
                            prompt_tokens = p_tokens
                            
                    if 'completion_tokens' in kwargs_dict:
                        c_tokens = kwargs_dict['completion_tokens']
                        if isinstance(c_tokens, str) and c_tokens.isdigit():
                            completion_tokens = int(c_tokens)
                        elif isinstance(c_tokens, int):
                            completion_tokens = c_tokens
                else:
                    # Use actual method name in log
                    logger.debug("Token Extraction (_handle_menu_query): No valid additional_kwargs found.")
            except Exception as token_err:
                 # Use actual method name in log
                logger.error(f"Error extracting token counts for _handle_menu_query: {token_err}")
                prompt_tokens = None # Ensure reset on error
                completion_tokens = None # Ensure reset on error
            # --- END: Robust Token Extraction (Corrected - Direct Access) ---

            # Store tokens in class variables
            self.last_prompt_tokens = prompt_tokens
            self.last_completion_tokens = completion_tokens
            
            # Updated log message
            logger.info(f"_handle_menu_query: Got response in {elapsed:.2f}s (prompt_tokens={prompt_tokens}, completion_tokens={completion_tokens})")
            
            return response.message.content
        except Exception as e:
            logger.error(f"_handle_menu_query: Error: {type(e).__name__}: {str(e)}")
            return f"I'm sorry, I had trouble providing menu information. Error: {str(e)}" # Plain text error
    
    async def _handle_order_query(self, query: str) -> tuple[str, list]:
        """Handle order-related actions"""
        order_template = f"""
        You are an assistant helping with food orders and guiding the user towards completing their purchase.
        **Use the provided conversation history to understand the current order status and context.**
        Be clear about prices and options. Summarize complex orders or options if necessary.
        Offer to provide more detail if needed.
        If they want something not on the menu, politely inform them it's unavailable.
        
        **Presenting Options (General):**
        - ALWAYS present choices with letter labels (A, B, C, etc.) at the beginning of each choice.
        - This applies to menu items, customization options, and any selection the user needs to make.
        - List each option on a new line.
        - Use standard text formatting only. Do NOT use markdown (like ** for bolding).
        - Example format for presenting drink sizes:
          Would you like:
          A. Small Coke ($2.00)
          B. Medium Coke ($2.50)
          C. Large Coke ($3.25)
        
        **Presenting Options (Item-Specific Add-ons/Modifications):**
        - **If the user adds an item that has specific options listed in the menu (like add cheese, add bacon, make it spicy for a sandwich):**
            - First, confirm the item was added (e.g., "Great choice! Added Classic Chicken Sandwich to your cart.").
            - Then, ask if they want to add any of *its specific options*.
            - List these specific options clearly WITH LETTER LABELS (A, B, C, etc.), including the price modifier (e.g., `+$.50`, `(no charge)`). Use plain text.
            - Example: 
              "Would you like to add any options?
              A. Add Cheese (+$1.00)
              B. Add Bacon (+$1.50)
              C. Make It Spicy (+$0.50)
              D. Substitute Grilled Chicken (no charge)
              Let me know if you want to add options, add more items, or checkout!"
        - **If the user adds an item with NO specific options listed in the menu:**
             - Simply confirm the item was added (e.g., "Okay, Regular Fries added.") and ask if they want to add anything else or checkout. Use plain text.

        In addition to your text response, you must also manage and return the user's cart state.
        You need to parse the user's intent and:
        1. For "add" - add items to the cart
        2. For "remove" - remove items from the cart
        3. For "change" - modify existing items (e.g., change quantity, options)
        4. For "upgrade" - upgrade items (e.g., size, add-ons)
        5. For "cancel order" - empty the cart
        6. For "make order" / "checkout" / "confirm" - This should be handled by the ORDER_CONFIRMATION intent, but acknowledge if the user explicitly mentions it here and potentially ask if they are ready to confirm.
        
        When responding, output BOTH:
        1. A conversational text message acknowledging the user's action.
           - After modifying the cart (add/remove/change), confirm the current state of the cart and ask if they want to add anything else or proceed to checkout. Use plain text.
        2. A valid JSON representation of their updated cart
        
        The cart should be a JSON array of objects with properties:
        - "item": string - the menu item name
        - "quantity": number - how many of this item
        - "options": array of strings - any options/modifications
        - "price": number - the unit price of this item including options
        
        FORMAT:
        {{
          "response": "Your plain text natural language response here. Guide towards checkout if appropriate.",
          "cart": [updated cart items]
        }}
        
        Based on the menu information and the user\'s request, help them place or modify their order.
        The complete menu is as follows:
        {self.menu_text}
        
        **Example Interaction (Item with Options):**
        User: "Can I have a Classic Chicken Sandwich?"
        Assistant Output (JSON):
        {{
          "response": "Great choice! Added Classic Chicken Sandwich ($8.99) to your cart. Would you like to add any options?\\nA. Add Cheese (+$1.00)\\nB. Add Bacon (+$1.50)\\nC. Make It Spicy (+$0.50)\\nD. Substitute Grilled Chicken (no charge)\\nOr let me know if you want to add more items or checkout!",
          "cart": [{{"item": "Classic Chicken Sandwich", "quantity": 1, "price": 8.99, "options": []}}]
        }}

        **Example Interaction (General Choice - Size):**
        User: "Add a coke"
        Assistant Output (JSON):
        {{
          "response": "Sure thing. We have:\\nA. Small Coke ($2.00)\\nB. Medium Coke ($2.50)\\nC. Large Coke ($3.25)\\nWhich one would you like?",
          "cart": [/* existing cart items */]
        }}
        """
        
        # Generate response
        messages = self.chat_history + [
            ChatMessage(role=MessageRole.USER, content=query),
            ChatMessage(role=MessageRole.SYSTEM, content=order_template)
        ]
        
        logger.info("_handle_order_query: Sending request to OpenAI")
        llm = OpenAI(model="gpt-4o", temperature=0.0, request_timeout=30)
        try:
            start_time = time.time()
            response = await llm.achat(messages)
            elapsed = time.time() - start_time
            
            # Detailed debug logging of the raw response object
            response_type = type(response).__name__
            has_additional_kwargs = hasattr(response, 'additional_kwargs')
            additional_kwargs_type = type(getattr(response, 'additional_kwargs', None)).__name__
            logger.info(f"DEBUG: Response type={response_type}, has_additional_kwargs={has_additional_kwargs}, additional_kwargs_type={additional_kwargs_type}")
            
            if has_additional_kwargs:
                additional_kwargs = response.additional_kwargs
                logger.info(f"DEBUG: additional_kwargs keys: {additional_kwargs.keys() if isinstance(additional_kwargs, dict) else 'Not a dict'}")
                if isinstance(additional_kwargs, dict) and 'token_usage' in additional_kwargs:
                    token_usage = additional_kwargs['token_usage']
                    logger.info(f"DEBUG: token_usage={token_usage}, type={type(token_usage)}")
            
            # --- START: Robust Token Extraction (Corrected - Direct Access) ---
            prompt_tokens = None
            completion_tokens = None
            try:
                if hasattr(response, 'additional_kwargs') and isinstance(response.additional_kwargs, dict):
                    kwargs_dict = response.additional_kwargs # Get the dictionary directly
                    if 'prompt_tokens' in kwargs_dict:
                        p_tokens = kwargs_dict['prompt_tokens']
                        if isinstance(p_tokens, str) and p_tokens.isdigit():
                            prompt_tokens = int(p_tokens)
                        elif isinstance(p_tokens, int):
                            prompt_tokens = p_tokens
                            
                    if 'completion_tokens' in kwargs_dict:
                        c_tokens = kwargs_dict['completion_tokens']
                        if isinstance(c_tokens, str) and c_tokens.isdigit():
                            completion_tokens = int(c_tokens)
                        elif isinstance(c_tokens, int):
                            completion_tokens = c_tokens
                else:
                    # Use actual method name in log
                    logger.debug("Token Extraction (_handle_order_query): No valid additional_kwargs found.")
            except Exception as token_err:
                 # Use actual method name in log
                logger.error(f"Error extracting token counts for _handle_order_query: {token_err}")
                prompt_tokens = None # Ensure reset on error
                completion_tokens = None # Ensure reset on error
            # --- END: Robust Token Extraction (Corrected - Direct Access) ---
    
            # Store tokens in class variables
            self.last_prompt_tokens = prompt_tokens
            self.last_completion_tokens = completion_tokens
            
            # Updated log message
            logger.info(f"_handle_order_query: Got response in {elapsed:.2f}s (prompt_tokens={prompt_tokens}, completion_tokens={completion_tokens})")
            
            # Parse response to extract cart information
            response_content = response.message.content
            cart_items = []
            
            try:
                # Try to parse the response as JSON
                if isinstance(response_content, str):
                    # Extract JSON object using regex for better reliability
                    import re
                    # Look for JSON objects in the text
                    json_matches = re.findall(r'(\{(?:[^{}]|(?:\{[^{}]*\}))*\})', response_content, re.DOTALL)
                    
                    if json_matches:
                        # Try each match until we find a valid JSON object with the expected structure
                        for json_str in json_matches:
                            try:
                                data = json.loads(json_str)
                                if isinstance(data, dict) and "response" in data:
                                    # Found a valid JSON object with "response" field
                                    response_content = data.get("response", "")
                                    # Extract cart items if available
                                    if "cart" in data and isinstance(data["cart"], list):
                                        cart_items = data["cart"]
                                        logger.info(f"Extracted cart items: {len(cart_items)} items")
                                    break  # Stop after finding the first valid match
                            except json.JSONDecodeError:
                                continue  # Try the next match
                    else:
                        logger.warning("No JSON objects found in the response")
                else:
                    logger.warning(f"Response content is not a string: {type(response_content)}")
            except Exception as e:
                logger.error(f"Error extracting cart data: {type(e).__name__}: {str(e)}")
                # Continue with original response content
            
            return response_content, cart_items
        except Exception as e:
            logger.error(f"_handle_order_query: Error: {type(e).__name__}: {str(e)}")
            return f"I'm sorry, I had trouble with your order. Error: {str(e)}", [] # Plain text error
    
    async def _handle_end_conversation(self, query: str) -> str:
        """Handle end of conversation"""
        end_template = """
        The user seems to be ending the conversation.
        **Consider the conversation history for context if appropriate (e.g., thanking them for an order).**
        Respond with a friendly, concise, **plain text** goodbye message that invites them to return. Do not use markdown.
        """
        
        messages = self.chat_history + [
            ChatMessage(role=MessageRole.USER, content=query),
            ChatMessage(role=MessageRole.SYSTEM, content=end_template)
        ]
        
        logger.info("_handle_end_conversation: Sending request to OpenAI")
        llm = OpenAI(model="gpt-4o", temperature=0.0, request_timeout=30)
        try:
            start_time = time.time()
            response = await llm.achat(messages)
            elapsed = time.time() - start_time
            
            # Detailed debug logging of the raw response object
            response_type = type(response).__name__
            has_additional_kwargs = hasattr(response, 'additional_kwargs')
            additional_kwargs_type = type(getattr(response, 'additional_kwargs', None)).__name__
            logger.info(f"DEBUG: Response type={response_type}, has_additional_kwargs={has_additional_kwargs}, additional_kwargs_type={additional_kwargs_type}")
            
            if has_additional_kwargs:
                additional_kwargs = response.additional_kwargs
                logger.info(f"DEBUG: additional_kwargs keys: {additional_kwargs.keys() if isinstance(additional_kwargs, dict) else 'Not a dict'}")
                if isinstance(additional_kwargs, dict) and 'token_usage' in additional_kwargs:
                    token_usage = additional_kwargs['token_usage']
                    logger.info(f"DEBUG: token_usage={token_usage}, type={type(token_usage)}")
            
            # --- START: Robust Token Extraction (Corrected - Direct Access) ---
            prompt_tokens = None
            completion_tokens = None
            try:
                if hasattr(response, 'additional_kwargs') and isinstance(response.additional_kwargs, dict):
                    kwargs_dict = response.additional_kwargs # Get the dictionary directly
                    if 'prompt_tokens' in kwargs_dict:
                        p_tokens = kwargs_dict['prompt_tokens']
                        if isinstance(p_tokens, str) and p_tokens.isdigit():
                            prompt_tokens = int(p_tokens)
                        elif isinstance(p_tokens, int):
                            prompt_tokens = p_tokens
                            
                    if 'completion_tokens' in kwargs_dict:
                        c_tokens = kwargs_dict['completion_tokens']
                        if isinstance(c_tokens, str) and c_tokens.isdigit():
                            completion_tokens = int(c_tokens)
                        elif isinstance(c_tokens, int):
                            completion_tokens = c_tokens
                else:
                    # Use actual method name in log
                    logger.debug("Token Extraction (_handle_end_conversation): No valid additional_kwargs found.")
            except Exception as token_err:
                logger.error(f"Error extracting token counts for _handle_end_conversation: {token_err}")
                prompt_tokens = None # Ensure reset on error
                completion_tokens = None # Ensure reset on error
            # --- END: Robust Token Extraction (Corrected - Direct Access) ---
            
            # Store tokens in class variables
            self.last_prompt_tokens = prompt_tokens
            self.last_completion_tokens = completion_tokens
            
            # Updated log message
            logger.info(f"_handle_end_conversation: Got response in {elapsed:.2f}s (prompt_tokens={prompt_tokens}, completion_tokens={completion_tokens})")
            return response.message.content
        except Exception as e:
            logger.error(f"_handle_end_conversation: Error: {type(e).__name__}: {str(e)}")
            return f"I'm sorry, I had trouble saying goodbye. Error: {str(e)}" # Plain text error

    async def _handle_order_confirmation(self, query: str) -> tuple:
        """Handle order confirmation actions"""
        current_cart = self.chat_history[-1].content if self.chat_history and hasattr(self.chat_history[-1], 'content') else "[]"
        
        # Try to extract cart from the conversation history
        try:
            cart_items = []
            # Look for cart items in the chat history
            for msg in reversed(self.chat_history):
                if hasattr(msg, 'content') and isinstance(msg.content, str):
                    # Look for JSON objects in the content
                    import re
                    json_matches = re.findall(r'(\{(?:[^{}]|(?:\{[^{}]*\}))*\})', msg.content, re.DOTALL)
                    for json_str in json_matches:
                        try:
                            data = json.loads(json_str)
                            if isinstance(data, dict) and "cart" in data and isinstance(data["cart"], list):
                                cart_items = data["cart"]
                                break
                        except json.JSONDecodeError:
                            continue
                if cart_items:  # Break outer loop if cart found
                    break
                    
            # Ensure all cart items are dictionaries
            validated_cart_items = []
            for item in cart_items:
                if isinstance(item, dict):
                    # Ensure required fields exist
                    if "item" not in item:
                        item["item"] = "Unknown item"
                    if "quantity" not in item:
                        item["quantity"] = 1
                    if "price" not in item:
                        item["price"] = 0.0
                    if "options" not in item or not isinstance(item["options"], list):
                        item["options"] = []
                    validated_cart_items.append(item)
                else:
                    logger.warning(f"Skipping invalid cart item: {item}")
            
            cart_items = validated_cart_items
                    
        except Exception as e:
            logger.error(f"Error extracting cart from history: {str(e)}")
            cart_items = []
            
        confirmation_template = f"""
        You are a helpful restaurant assistant confirming an order.
        
        **User's current message:** "{query}"
        
        **Order confirmation context:**
        When the user says messages like "nothing else", "that's all", "I'm done", or "checkout", this means they want to finish ordering and are ready to confirm their order. If this is the case, summarize their order and ask for final confirmation.
        
        When the user says things like "yes", "confirm", "approved", "correct", or similar phrases, they are confirming their order. If you determine this is a confirmation, mark the order as CONFIRMED and thank them.
        
        **Instructions:**
        1. If the cart is empty, tell them they need to add items first.
        2. If there are items in the cart and the user is finishing their order without explicitly confirming it: 
           - Start the response with "Your order:".
           - List EACH item from the `cart`. Use plain text (no markdown). Include quantity, any options (concisely), and the item's calculated price (quantity * unit price). Use a bulleted list (`- ` or `* `) for the items.
           - After listing ALL items, include the total price (e.g., "Total: $XX.XX").
           - Ask for confirmation with LETTER CHOICES: "Would you like to confirm this order?\\nA. Yes, confirm my order\\nB. No, I'd like to make changes"
           - Set status to "PENDING CONFIRMATION".
        3. If the user is explicitly confirming a previous confirmation request, thank them, set status to "CONFIRMED". Use plain text.
        
        FORMAT (empty cart):
        {{
          "response": "Your cart is empty. Please add items before confirming your order.",
          "cart": [],
          "cart_status": "OPEN"
        }}
        
        FORMAT (items in cart, user is finishing order but hasn't confirmed):
        {{
          "response": "Your order:\\n- 1 Classic Chicken Sandwich (extra cheese): $9.99\\n- 2 Sodas (Large): $6.50\\n\\nTotal: $16.49\\n\\nWould you like to confirm this order?\\nA. Yes, confirm my order\\nB. No, I'd like to make changes",
          "cart": [existing cart items],
          "cart_status": "PENDING CONFIRMATION"
        }}
        
        FORMAT (user confirming order):
        {{
          "response": "Thank you! Your order has been confirmed and will be ready shortly.",
          "cart": [existing cart items],
          "cart_status": "CONFIRMED"
        }}
        
        Based on the cart contents and user message, provide the appropriate plain text response. Ensure the total price is calculated correctly.
        IMPORTANT: Each cart item MUST be a dictionary with "item", "quantity", "price", and "options" fields. The "options" field must be a list.
        Example of a valid cart item: {{\"item\": \"Burger\", \"quantity\": 1, \"price\": 8.99, \"options\": [\"extra cheese\"]}}
        """
        
        # Generate response
        messages = self.chat_history + [
            ChatMessage(role=MessageRole.USER, content=query),
            ChatMessage(role=MessageRole.SYSTEM, content=confirmation_template)
        ]
        
        logger.info("_handle_order_confirmation: Sending request to OpenAI")
        llm = OpenAI(model="gpt-4o", temperature=0.0, request_timeout=30)
        try:
            start_time = time.time()
            response = await llm.achat(messages)
            elapsed = time.time() - start_time
            
            # Detailed debug logging of the raw response object
            response_type = type(response).__name__
            has_additional_kwargs = hasattr(response, 'additional_kwargs')
            additional_kwargs_type = type(getattr(response, 'additional_kwargs', None)).__name__
            logger.info(f"DEBUG: Response type={response_type}, has_additional_kwargs={has_additional_kwargs}, additional_kwargs_type={additional_kwargs_type}")
            
            if has_additional_kwargs:
                additional_kwargs = response.additional_kwargs
                logger.info(f"DEBUG: additional_kwargs keys: {additional_kwargs.keys() if isinstance(additional_kwargs, dict) else 'Not a dict'}")
                if isinstance(additional_kwargs, dict) and 'token_usage' in additional_kwargs:
                    token_usage = additional_kwargs['token_usage']
                    logger.info(f"DEBUG: token_usage={token_usage}, type={type(token_usage)}")
            
            # --- START: Robust Token Extraction (Corrected - Direct Access) ---
            prompt_tokens = None
            completion_tokens = None
            try:
                if hasattr(response, 'additional_kwargs') and isinstance(response.additional_kwargs, dict):
                    kwargs_dict = response.additional_kwargs # Get the dictionary directly
                    if 'prompt_tokens' in kwargs_dict:
                        p_tokens = kwargs_dict['prompt_tokens']
                        if isinstance(p_tokens, str) and p_tokens.isdigit():
                            prompt_tokens = int(p_tokens)
                        elif isinstance(p_tokens, int):
                            prompt_tokens = p_tokens
                            
                    if 'completion_tokens' in kwargs_dict:
                        c_tokens = kwargs_dict['completion_tokens']
                        if isinstance(c_tokens, str) and c_tokens.isdigit():
                            completion_tokens = int(c_tokens)
                        elif isinstance(c_tokens, int):
                            completion_tokens = c_tokens
                else:
                    # Use actual method name in log
                    logger.debug("Token Extraction (_handle_order_confirmation): No valid additional_kwargs found.")
            except Exception as token_err:
                logger.error(f"Error extracting token counts for _handle_order_confirmation: {token_err}")
                prompt_tokens = None # Ensure reset on error
                completion_tokens = None # Ensure reset on error
            # --- END: Robust Token Extraction (Corrected - Direct Access) ---
            
            # Store tokens in class variables
            self.last_prompt_tokens = prompt_tokens
            self.last_completion_tokens = completion_tokens
            
            # Updated log message
            logger.info(f"_handle_order_confirmation: Got response in {elapsed:.2f}s (prompt_tokens={prompt_tokens}, completion_tokens={completion_tokens})")
            
            # Parse response to extract cart and status information
            response_content = response.message.content
            # Use the validated cart items as a fallback
            final_cart_items = cart_items.copy()
            cart_status = "OPEN"  # Default status
            
            try:
                # Try to parse the response as JSON
                if isinstance(response_content, str):
                    # Extract JSON object using regex for better reliability
                    import re
                    # Look for JSON objects in the text
                    json_matches = re.findall(r'(\{(?:[^{}]|(?:\{[^{}]*\}))*\})', response_content, re.DOTALL)
                    
                    if json_matches:
                        # Try each match until we find a valid JSON object with the expected structure
                        for json_str in json_matches:
                            try:
                                data = json.loads(json_str)
                                if isinstance(data, dict) and "response" in data:
                                    # Found a valid JSON object with "response" field
                                    response_content = data.get("response", "")
                                    # Extract cart items if available
                                    if "cart" in data and isinstance(data["cart"], list):
                                        extracted_cart = data["cart"]
                                        # Validate extracted cart items
                                        validated_extracted_cart = []
                                        for item in extracted_cart:
                                            if isinstance(item, dict):
                                                # Ensure required fields exist
                                                if "item" not in item:
                                                    item["item"] = "Unknown item"
                                                if "quantity" not in item:
                                                    item["quantity"] = 1
                                                if "price" not in item:
                                                    item["price"] = 0.0
                                                if "options" not in item or not isinstance(item["options"], list):
                                                    item["options"] = []
                                                validated_extracted_cart.append(item)
                                            else:
                                                logger.warning(f"Skipping invalid extracted cart item: {item}")
                                        final_cart_items = validated_extracted_cart
                                        logger.info(f"Extracted cart items: {len(final_cart_items)} items")
                                    # Extract cart status if available
                                    if "cart_status" in data and isinstance(data["cart_status"], str):
                                        cart_status = data["cart_status"]
                                        logger.info(f"Extracted cart status: {cart_status}")
                                    break  # Stop after finding the first valid match
                            except json.JSONDecodeError:
                                continue  # Try the next match
                    else:
                        logger.warning("No JSON objects found in the response")
                else:
                    logger.warning(f"Response content is not a string: {type(response_content)}")
            except Exception as e:
                logger.error(f"Error extracting cart/status data: {type(e).__name__}: {str(e)}")
                # Continue with original response content
            
            return response_content, final_cart_items, cart_status
        except Exception as e:
            logger.error(f"_handle_order_confirmation: Error: {type(e).__name__}: {str(e)}")
            return f"I'm sorry, I had trouble confirming your order. Error: {str(e)}", [], "OPEN" # Plain text error

def create_chat_engine(menu: Dict[str, Dict[str, Any]], chat_history: List[Dict[str, str]] = None):
    """
    Create a chat workflow that follows the architectural diagram.
    
    Args:
        menu: Dictionary containing menu items
        chat_history: List of previous chat messages
    
    Returns:
        A workflow object that can be used to process chat messages
    """
    formatted_chat_history = []
    if chat_history:
        # Keep only the most recent 20 messages
        recent_history = chat_history[-20:]
        logger.info(f"Using the last {len(recent_history)} messages out of {len(chat_history)} for history.")
        for message in recent_history:
            role = MessageRole.USER if message["role"] == "user" else MessageRole.ASSISTANT
            formatted_chat_history.append(ChatMessage(role=role, content=message["content"]))
    
    # Create and return workflow with an extended timeout (60 seconds)
    workflow = FoodOrderingWorkflow(
        menu=menu, 
        chat_history=formatted_chat_history,
        timeout=60.0  # Increase the timeout to 60 seconds
    )
    
    # The @step decorators automatically register and link the steps based on type hints.
    # Explicit add_step calls are not needed here.
    
    return workflow 