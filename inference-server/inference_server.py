#!/usr/bin/env python3
"""
Generic Inference Server - Fixed Version
- Removed hardcoded poetry system prompt
- Better conversation formatting
- Configurable models and generation parameters
- Model availability checking with configurable error responses
"""

import sys
import json
import os
import traceback
import uuid
import time
import re

# ═══════════════════════════════════════════════════════════════════
# Configuration Variables
# ═══════════════════════════════════════════════════════════════════

# Read configuration from environment or command line
MODEL_TYPE = os.environ.get("MODEL_TYPE", "huggingface")  # huggingface, ollama, etc.
MODEL_NAME = os.environ.get("MODEL_NAME", "TinyLlama/TinyLlama-1.1B-Chat-v1.0")
DEFAULT_TEMPERATURE = float(os.environ.get("DEFAULT_TEMPERATURE", "0.7"))
DEFAULT_MAX_LENGTH = int(os.environ.get("DEFAULT_MAX_LENGTH", "200"))

# Model availability configuration
SHOW_AVAILABLE_MODELS = os.environ.get("SHOW_AVAILABLE_MODELS", "true").lower() == "true"
MODEL_NOT_FOUND_MESSAGE = os.environ.get("MODEL_NOT_FOUND_MESSAGE", "Model not available")

# Define available models for each backend type
if MODEL_TYPE == "huggingface":
    # Configure available HuggingFace models
    AVAILABLE_MODELS = [
        "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
        # Add more models here as you install them
        # "microsoft/DialoGPT-medium",
        # "google/flan-t5-small",
        # "facebook/blenderbot-400M-distill",
    ]
elif MODEL_TYPE == "ollama":
    # Configure available Ollama models
    AVAILABLE_MODELS = [
            "mistral:7b-instruct-q4_K_M"
        # Add more models here as you pull them in Ollama
    ]
else:
    AVAILABLE_MODELS = [MODEL_NAME]  # Default fallback

print(f"Starting Inference Server with {MODEL_TYPE}:{MODEL_NAME}")
print(f"Default temperature: {DEFAULT_TEMPERATURE}, Max length: {DEFAULT_MAX_LENGTH}")
print(f"Available models: {AVAILABLE_MODELS}")
print(f"Show available models on error: {SHOW_AVAILABLE_MODELS}")
sys.stdout.flush()

def check_model_availability(requested_model: str) -> tuple[bool, str]:
    """
    Check if a model is available and return appropriate response.
    
    Returns:
        tuple: (is_available, error_message_if_not_available)
    """
    if requested_model in AVAILABLE_MODELS:
        return True, ""
    
    if SHOW_AVAILABLE_MODELS:
        available_list = ", ".join(AVAILABLE_MODELS)
        error_msg = f"{MODEL_NOT_FOUND_MESSAGE}. Available models: {available_list}"
    else:
        error_msg = MODEL_NOT_FOUND_MESSAGE
    
    return False, error_msg

def send_error_response(request_id: str, error_message: str):
    """Send an error response in the expected format"""
    error_response = {
        "request_id": request_id,
        "error": error_message,
        "model_type": MODEL_TYPE,
        "timestamp": time.time()
    }
    print(json.dumps(error_response))
    sys.stdout.flush()

def clean_response(text: str, original_prompt: str) -> str:
    """Clean up the generated response by removing the original prompt and extra formatting"""
    # Remove the original prompt if it appears at the start
    if text.startswith(original_prompt):
        text = text[len(original_prompt):].strip()
    
    # Remove common conversation markers that might be generated
    text = re.sub(r'^(User:|Human:|Assistant:|AI:)\s*', '', text, flags=re.MULTILINE)
    
    # Clean up extra whitespace and newlines
    text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)  # Remove triple+ newlines
    text = text.strip()
    
    # If response is empty or too short, provide a fallback
    if not text or len(text.strip()) < 3:
        text = "I understand. How can I help you today?"
    
    return text

def format_conversation_prompt(prompt: str) -> str:
    """
    Format prompts for better conversation handling.
    Handles both simple prompts and structured conversation formats.
    """
    # If the prompt already has conversation structure, use it as-is
    if any(marker in prompt for marker in ["User:", "Human:", "Assistant:", "System:"]):
        # Already formatted - just ensure it ends properly for completion
        if not prompt.rstrip().endswith(("Assistant:", "AI:")):
            if "Assistant:" in prompt:
                prompt += "\nAssistant:"
            elif "AI:" in prompt:
                prompt += "\nAI:"
            else:
                prompt += "\nAssistant:"
        return prompt
    
    # Simple prompt - format it as a user message
    return f"User: {prompt}\nAssistant:"

def extract_system_message(prompt: str) -> tuple[str, str]:
    """Extract system message if present and return (system_msg, remaining_prompt)"""
    lines = prompt.split('\n')
    system_msg = ""
    remaining_lines = []
    
    for line in lines:
        if line.strip().startswith("System:"):
            system_msg = line.replace("System:", "").strip()
        else:
            remaining_lines.append(line)
    
    return system_msg, '\n'.join(remaining_lines)

# Initialize the model based on type
if MODEL_TYPE == "huggingface":
    print("Initializing Hugging Face model...")
    sys.stdout.flush()
    
    # Check if configured model is in available list
    if MODEL_NAME not in AVAILABLE_MODELS:
        print(f"❌ Error: Configured model '{MODEL_NAME}' not in available models list")
        print(f"Available models: {AVAILABLE_MODELS}")
        sys.exit(1)
    
    from transformers import AutoModelForCausalLM, AutoTokenizer
    import torch
    
    try:
        tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
        
        # Add padding token if it doesn't exist
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_NAME,
            torch_dtype="auto",  # Use appropriate dtype
            device_map="auto",   # Automatically handle device placement
        )
        print("✅ Model loaded successfully")
        
    except Exception as e:
        print(f"❌ Error loading model: {e}")
        sys.exit(1)
    
    def generate_text(prompt: str, model_name: str = None, temperature: float = None, max_length: int = None) -> str:
        """Generate text using Hugging Face transformers"""
        try:
            # Check if specific model was requested and if it's available
            requested_model = model_name or MODEL_NAME
            is_available, error_msg = check_model_availability(requested_model)
            
            if not is_available:
                return f"ERROR: {error_msg}"
            
            # If a different model than the loaded one is requested, return error
            if requested_model != MODEL_NAME:
                return f"ERROR: Requested model '{requested_model}' is available but not currently loaded. Currently loaded: '{MODEL_NAME}'"
            
            # Use provided parameters or defaults
            temp = temperature if temperature is not None else DEFAULT_TEMPERATURE
            max_len = max_length if max_length is not None else DEFAULT_MAX_LENGTH
            
            # Format the prompt properly
            formatted_prompt = format_conversation_prompt(prompt)
            
            # Extract system message if present (though TinyLlama doesn't specifically handle it)
            system_msg, conversation_prompt = extract_system_message(formatted_prompt)
            
            # Use the conversation prompt for generation
            inputs = tokenizer(conversation_prompt, return_tensors="pt", padding=True, truncation=True)
            
            # Generation parameters - balanced for good conversation
            generation_kwargs = {
                "max_new_tokens": min(max_len, 150),  # Limit new tokens to prevent repetition
                "temperature": max(0.1, min(temp, 1.5)),  # Keep temperature reasonable
                "top_p": 0.9,
                "top_k": 50,
                "do_sample": True,
                "num_return_sequences": 1,
                "pad_token_id": tokenizer.eos_token_id,
                "repetition_penalty": 1.1,  # Reduce repetition
                "length_penalty": 1.0,
                "early_stopping": True
            }
            
            # Move inputs to same device as model
            if hasattr(model, 'device'):
                inputs = {k: v.to(model.device) for k, v in inputs.items()}
            
            with torch.no_grad():
                outputs = model.generate(**inputs, **generation_kwargs)
            
            # Decode the generated text
            generated_text = tokenizer.decode(outputs[0], skip_special_tokens=True)
            
            # Clean up the response
            cleaned_response = clean_response(generated_text, conversation_prompt)
            
            return cleaned_response
            
        except Exception as e:
            print(f"❌ Generation error: {e}")
            traceback.print_exc()
            return "I apologize, but I encountered an error while generating a response. Please try again."

elif MODEL_TYPE == "ollama":
    print("Initializing Ollama...")
    sys.stdout.flush()
    
    # Check if configured model is in available list
    if MODEL_NAME not in AVAILABLE_MODELS:
        print(f"❌ Error: Configured model '{MODEL_NAME}' not in available models list")
        print(f"Available models: {AVAILABLE_MODELS}")
        sys.exit(1)
    
    import requests
    
    def generate_text(prompt: str, model_name: str = None, temperature: float = None, max_length: int = None) -> str:
        """Generate text using Ollama API"""
        try:
            # Check if specific model was requested and if it's available
            requested_model = model_name or MODEL_NAME
            is_available, error_msg = check_model_availability(requested_model)
            
            if not is_available:
                return f"ERROR: {error_msg}"
            
            temp = temperature if temperature is not None else DEFAULT_TEMPERATURE
            max_len = max_length if max_length is not None else DEFAULT_MAX_LENGTH
            
            formatted_prompt = format_conversation_prompt(prompt)
            system_msg, conversation_prompt = extract_system_message(formatted_prompt)
            
            # Prepare request for Ollama
            request_data = {
                "model": requested_model,  # Use the requested model
                "prompt": conversation_prompt,
                "temperature": temp,
                "max_tokens": max_len,
                "stream": False
            }
            
            # Add system message if present
            if system_msg:
                request_data["system"] = system_msg
            
            response = requests.post(
                "http://localhost:11434/api/generate",
                json=request_data,
                timeout=60
            )
            
            if response.status_code == 200:
                result = response.json()
                generated_text = result.get("response", "")
                return clean_response(generated_text, conversation_prompt)
            else:
                return f"ERROR: Ollama API returned status {response.status_code}"
                
        except Exception as e:
            print(f"❌ Ollama generation error: {e}")
            return f"ERROR: I encountered an error while generating a response: {e}"
        
else:
    print(f"❌ Unknown model type: {MODEL_TYPE}")
    sys.exit(1)

print("✅ Model initialized and ready!")
print("Waiting for inference requests...")
sys.stdout.flush()

# Main inference loop
try:
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        
        try:
            data = json.loads(line.strip())
            
            # Extract parameters
            prompt = data.get("prompt", "Hello")
            model_name = data.get("model")  # Model can be specified per request
            temperature = data.get("temperature")
            max_tokens = data.get("max_tokens")
            request_id = data.get("request_id", str(uuid.uuid4()))
            
            print(f"📥 Processing request {request_id}: {prompt[:50]}...")
            if model_name:
                print(f"   Requested model: {model_name}")
            sys.stdout.flush()
            
            start_time = time.time()
            
            # Generate response
            response = generate_text(prompt, model_name, temperature, max_tokens)
            
            generation_time = time.time() - start_time
            
            # Check if response is an error
            if response.startswith("ERROR:"):
                # Send error response
                error_response = {
                    "request_id": request_id,
                    "error": response[7:],  # Remove "ERROR: " prefix
                    "model": model_name or MODEL_NAME,
                    "model_type": MODEL_TYPE,
                    "available_models": AVAILABLE_MODELS if SHOW_AVAILABLE_MODELS else None,
                    "timestamp": time.time()
                }
                print(json.dumps(error_response))
                sys.stdout.flush()
                print(f"❌ Request {request_id} failed: {response}")
                sys.stderr.flush()
                continue
            
            # Prepare successful response
            result = {
                "request_id": request_id,
                "response": response,
                "model": model_name or MODEL_NAME,
                "model_type": MODEL_TYPE,
                "generation_time": generation_time,
                "timestamp": time.time()
            }
            
            # Output response
            print(json.dumps(result))
            sys.stdout.flush()
            
            print(f"✅ Request {request_id} completed in {generation_time:.2f}s")
            sys.stderr.flush()
            
        except json.JSONDecodeError as e:
            error_response = {
                "error": f"Invalid JSON input: {e}",
                "model_type": MODEL_TYPE,
                "timestamp": time.time()
            }
            print(json.dumps(error_response))
            sys.stdout.flush()
            
        except Exception as e:
            print(f"❌ Error processing request: {e}", file=sys.stderr)
            traceback.print_exc()
            error_response = {
                "error": f"Processing error: {e}",
                "model_type": MODEL_TYPE,
                "timestamp": time.time()
            }
            print(json.dumps(error_response))
            sys.stdout.flush()

except KeyboardInterrupt:
    print("\n🛑 Inference server shutting down...")
except Exception as e:
    print(f"❌ Fatal error: {e}")
    traceback.print_exc()
    sys.exit(1)
