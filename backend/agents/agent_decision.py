"""
Agent Decision System for Multi-Agent Medical Chatbot

This module handles the orchestration of different agents using LangGraph.
It dynamically routes user queries to the appropriate agent based on content and context.
"""

import json
from typing import Dict, List, Optional, Any, Literal, TypedDict, Union, Annotated
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnablePassthrough
from langgraph.graph import MessagesState, StateGraph, END
import os, getpass
from dotenv import load_dotenv
from agents.rag_agent import MedicalRAG
from agents.web_search_processor_agent import WebSearchProcessorAgent
from agents.image_analysis_agent import ImageAnalysisAgent
from agents.guardrails.local_guardrails import LocalGuardrails

from langgraph.checkpoint.memory import MemorySaver

import cv2
import numpy as np

from config import Config

load_dotenv()

# Load configuration
config = Config()

# Initialize memory
memory = MemorySaver()

# Specify a thread
thread_config = {"configurable": {"thread_id": "1"}}


# Agent that takes the decision of routing the request further to correct task specific agent
class AgentConfig:
    """Configuration settings for the agent decision system."""
    
    # Decision model
    DECISION_MODEL = "gpt-4o"  # or whichever model you prefer
    
    # Vision model for image analysis
    VISION_MODEL = "gpt-4o"
    
    # Confidence threshold for responses
    CONFIDENCE_THRESHOLD = 0.85
    
    # System instructions for the decision agent
    DECISION_SYSTEM_PROMPT = """You are an intelligent medical triage system that routes user queries to 
    the appropriate specialized agent. Your job is to analyze the user's request and determine which agent 
    is best suited to handle it based on the query content, presence of images, and conversation context.

    Available agents:
    1. CONVERSATION_AGENT - For general chat, greetings, and non-medical questions.
    2. RAG_AGENT - For specific medical knowledge questions that can be answered from established medical literature. Currently ingested medical knowledge involves 'introduction to brain tumor', 'deep learning techniques to diagnose and detect brain tumors', 'deep learning techniques to diagnose and detect covid / covid-19 from chest x-ray'.
    3. WEB_SEARCH_PROCESSOR_AGENT - For questions about recent medical developments, current outbreaks, or time-sensitive medical information.
    4. BRAIN_TUMOR_AGENT - For analysis of brain MRI images to detect and segment tumors.
    5. CHEST_XRAY_AGENT - For analysis of chest X-ray images to detect abnormalities.
    6. SKIN_LESION_AGENT - For analysis of skin lesion images to classify them as benign or malignant.

    Make your decision based on these guidelines:
    - If the user has not uploaded any image, always route to the conversation agent.
    - If the user uploads a medical image, decide which medical vision agent is appropriate based on the image type and the user's query. If the image is uploaded without a query, always route to the correct medical vision agent based on the image type.
    - If the user asks about recent medical developments or current health situations, use the web search pocessor agent.
    - If the user asks specific medical knowledge questions, use the RAG agent.
    - For general conversation, greetings, or non-medical questions, use the conversation agent. But if image is uploaded, always go to the medical vision agents first.

    You must provide your answer in JSON format with the following structure:
    {{
    "agent": "AGENT_NAME",
    "reasoning": "Your step-by-step reasoning for selecting this agent",
    "confidence": 0.95  // Value between 0.0 and 1.0 indicating your confidence in this decision
    }}
    """

    image_analyzer = ImageAnalysisAgent(config=config)


class AgentState(MessagesState):
    """State maintained across the workflow."""
    # messages: List[BaseMessage]  # Conversation history
    agent_name: Optional[str]  # Current active agent
    current_input: Optional[Union[str, Dict]]  # Input to be processed
    has_image: bool  # Whether the current input contains an image
    image_type: Optional[str]  # Type of medical image if present
    output: Optional[str]  # Final output to user
    needs_human_validation: bool  # Whether human validation is required
    retrieval_confidence: float  # Confidence in retrieval (for RAG agent)
    bypass_routing: bool  # Flag to bypass agent routing for guardrails
    insufficient_info: bool  # Flag indicating RAG response has insufficient information
    input_lang: str  # Detected language of the input


class AgentDecision(TypedDict):
    """Output structure for the decision agent."""
    agent: str
    reasoning: str
    confidence: float


def create_agent_graph():
    """Create and configure the LangGraph for agent orchestration."""

    # Initialize guardrails with the same LLM used elsewhere
    guardrails = LocalGuardrails(config.rag.llm)

    # LLM
    decision_model = config.agent_decision.llm
    
    # Initialize the output parser
    json_parser = JsonOutputParser(pydantic_object=AgentDecision)
    
    # Create the decision prompt
    decision_prompt = ChatPromptTemplate.from_messages([
        ("system", AgentConfig.DECISION_SYSTEM_PROMPT),
        ("human", "{input}")
    ])
    
    # Create the decision chain
    decision_chain = decision_prompt | decision_model | json_parser
    
    # Define graph state transformations
    def analyze_input(state: AgentState) -> AgentState:
        """Analyze the input to detect images and determine input type."""
        current_input = state["current_input"]
        has_image = False
        image_type = None
        
        # Get the text from the input
        input_text = ""
        if isinstance(current_input, str):
            input_text = current_input
        elif isinstance(current_input, dict):
            input_text = current_input.get("text", "")
        
        # Detect language of user input
        input_lang = 'vi'  # Default to Vietnamese
        if input_text:
            input_lang = detect_language(input_text)
        
        # Check input through guardrails if text is present
        if input_text:
            is_allowed, message = guardrails.check_input(input_text)
            if not is_allowed:
                # If input is blocked, return early with guardrail message
                print(f"Selected agent: INPUT GUARDRAILS, Message: ", message)
                
                # Translate the guardrail message if needed
                if input_lang != 'en':
                    message = translate_text(message, input_lang)
                
                return {
                    **state,
                    "messages": message,
                    "agent_name": "INPUT_GUARDRAILS",
                    "has_image": False,
                    "image_type": None,
                    "bypass_routing": True,  # flag to end flow
                    "input_lang": input_lang  # Store the detected language
                }
        
        # Original image processing code
        if isinstance(current_input, dict) and "image" in current_input:
            has_image = True
            image_path = current_input.get("image", None)
            image_type_response = AgentConfig.image_analyzer.analyze_image(image_path)
            image_type = image_type_response['image_type']
            print("ANALYZED IMAGE TYPE: ", image_type)
        
        return {
            **state,
            "has_image": has_image,
            "image_type": image_type,
            "bypass_routing": False,  # Explicitly set to False for normal flow
            "input_lang": input_lang  # Store the detected language
        }
    
    def check_if_bypassing(state: AgentState) -> str:
        """Check if we should bypass normal routing due to guardrails."""
        if state.get("bypass_routing", False):
            return "apply_guardrails"
        return "route_to_agent"
    
    def route_to_agent(state: AgentState) -> Dict:
        """Make decision about which agent should handle the query."""
        messages = state["messages"]
        current_input = state["current_input"]
        has_image = state["has_image"]
        image_type = state["image_type"]
        
        # Prepare input for decision model
        input_text = ""
        if isinstance(current_input, str):
            input_text = current_input
        elif isinstance(current_input, dict):
            input_text = current_input.get("text", "")
        
        # Create context from recent conversation history (last 3 messages)
        recent_context = ""
        for msg in messages[-6:]:  # Get last 3 exchanges (6 messages)  # Not provided control from config
            if isinstance(msg, HumanMessage):
                recent_context += f"User: {msg.content}\n"
            elif isinstance(msg, AIMessage):
                recent_context += f"Assistant: {msg.content}\n"
        
        # Combine everything for the decision input
        decision_input = f"""
        User query: {input_text}

        Recent conversation context:
        {recent_context}

        Has image: {has_image}
        Image type: {image_type if has_image else 'None'}

        Based on this information, which agent should handle this query?
        """
        
        # Make the decision
        decision = decision_chain.invoke({"input": decision_input})

        # Decided agent
        print(f"Decision: {decision['agent']}")
        
        # Update state with decision
        updated_state = {
            **state,
            "agent_name": decision["agent"],
        }
        
        # Route based on agent name and confidence
        if decision["confidence"] < AgentConfig.CONFIDENCE_THRESHOLD:
            return {"agent_state": updated_state, "next": "needs_validation"}
        
        return {"agent_state": updated_state, "next": decision["agent"]}

    # Define agent execution functions (these will be implemented in their respective modules)
    def run_conversation_agent(state: AgentState) -> AgentState:
        """Handle general conversation."""

        print(f"Selected agent: CONVERSATION_AGENT")

        messages = state["messages"]
        current_input = state["current_input"]
        
        # Get input language from state
        input_lang = state.get("input_lang", "vi")
        
        # Get query text
        input_text = ""
        if isinstance(current_input, str):
            input_text = current_input
        elif isinstance(current_input, dict):
            input_text = current_input.get("text", "")
        
        # Create context from recent conversation history
        recent_context = ""
        for msg in messages:#[-20:]:  # Get last 10 exchanges (20 messages)  # currently considering complete history - limit control from config
            if isinstance(msg, HumanMessage):
                # print("######### DEBUG 1:", msg)
                recent_context += f"User: {msg.content}\n"
            elif isinstance(msg, AIMessage):
                # print("######### DEBUG 2:", msg)
                recent_context += f"Assistant: {msg.content}\n"
        
        # Combine everything for the decision input
        conversation_prompt = f"""User query: {input_text}

        Recent conversation context: {recent_context}

        You are an AI-powered Medical Conversation Assistant. Your goal is to facilitate smooth and informative conversations with users, handling both casual and medical-related queries. You must respond naturally while ensuring medical accuracy and clarity.

        ### Role & Capabilities
        - Engage in **general conversation** while maintaining professionalism.
        - Answer **medical questions** using verified knowledge.
        - Route **complex queries** to RAG (retrieval-augmented generation) or web search if needed.
        - Handle **follow-up questions** while keeping track of conversation context.
        - Redirect **medical images** to the appropriate AI analysis agent.

        ### Guidelines for Responding:
        1. **General Conversations:**
        - If the user engages in casual talk (e.g., greetings, small talk), respond in a friendly, engaging manner.
        - Keep responses **concise and engaging**, unless a detailed answer is needed.

        2. **Medical Questions:**
        - If you have **high confidence** in answering, provide a medically accurate response.
        - Ensure responses are **clear, concise, and factual**.

        3. **Follow-Up & Clarifications:**
        - Maintain conversation history for better responses.
        - If a query is unclear, ask **follow-up questions** before answering.

        4. **Handling Medical Image Analysis:**
        - Do **not** attempt to analyze images yourself.
        - If user speaks about analyzing or processing or detecting or segmenting or classifying any disease from any image, ask the user to upload the image so that in the next turn it is routed to the appropriate medical vision agents.
        - If an image was uploaded, it would have been routed to the medical computer vision agents. Read the history to know about the diagnosis results and continue conversation if user asks anything regarding the diagnosis.
        - After processing, **help the user interpret the results**.

        5. **Uncertainty & Ethical Considerations:**
        - If unsure, **never assume** medical facts.
        - Recommend consulting a **licensed healthcare professional** for serious medical concerns.
        - Avoid providing **medical diagnoses** or **prescriptions**—stick to general knowledge.

        ### Response Format:
        - Maintain a **conversational yet professional tone**.
        - Use **bullet points or numbered lists** for clarity when needed.
        - If pulling from external sources (RAG/Web Search), mention **where the information is from** (e.g., "According to Mayo Clinic...").
        - If a user asks for a diagnosis, remind them to **seek medical consultation**.

        ### Example User Queries & Responses:

        **User:** "Hey, how's your day going?"
        **You:** "I'm here and ready to help! How can I assist you today?"

        **User:** "I have a headache and fever. What should I do?"
        **You:** "I'm not a doctor, but headaches and fever can have various causes, from infections to dehydration. If your symptoms persist, you should see a medical professional."

        Conversational LLM Response:"""

        # print("Conversation Prompt:", conversation_prompt)

        response = config.conversation.llm.invoke(conversation_prompt)

        # Translate response if needed
        if input_lang != 'en':
            response.content = translate_text(response.content, input_lang)

        # print("Conversation respone:", response)

        # response = AIMessage(content="This would be handled by the conversation agent.")

        return {
            **state,
            "output": response,
            "agent_name": "CONVERSATION_AGENT"
        }
    
    def run_rag_agent(state: AgentState) -> AgentState:
        """Handle medical knowledge queries using RAG."""
        # Initialize the RAG agent

        print(f"Selected agent: RAG_AGENT")

        rag_agent = MedicalRAG(config)
        
        messages = state["messages"]
        query = state["current_input"]
        rag_context_limit = config.rag.context_limit

        # Get input language from state
        input_lang = state.get("input_lang", "vi")

        recent_context = ""
        for msg in messages[-rag_context_limit:]:# limit controlled from config
            if isinstance(msg, HumanMessage):
                # print("######### DEBUG 1:", msg)
                recent_context += f"User: {msg.content}\n"
            elif isinstance(msg, AIMessage):
                # print("######### DEBUG 2:", msg)
                recent_context += f"Assistant: {msg.content}\n"

        response = rag_agent.process_query(query, chat_history=recent_context)
        retrieval_confidence = response.get("confidence", 0.0)  # Default to 0.0 if not provided

        print(f"Retrieval Confidence: {retrieval_confidence}")
        print(f"Sources: {len(response['sources'])}")

        # Check if response indicates insufficient information
        insufficient_info = False
        response_content = response["response"]
        
        # Extract the content properly based on type
        if isinstance(response_content, dict) and hasattr(response_content, 'content'):
            # If it's an AIMessage or similar object with a content attribute
            response_text = response_content.content
        else:
            # If it's already a string
            response_text = response_content
            
        print(f"Response text type: {type(response_text)}")
        print(f"Response text preview: {response_text[:100]}...")
        
        if isinstance(response_text, str) and (
            "I don't have enough information to answer this question based on the provided context" in response_text or 
            "I don't have enough information" in response_text or 
            "don't have enough information" in response_text.lower() or
            "not enough information" in response_text.lower() or
            "insufficient information" in response_text.lower() or
            "cannot answer" in response_text.lower() or
            "unable to answer" in response_text.lower()
            ):
            
            print("RAG response indicates insufficient information")
            print(f"Response text that triggered insufficient_info: {response_text[:100]}...")
            insufficient_info = True

        print(f"Insufficient info flag set to: {insufficient_info}")

        # Translate response if needed
        if input_lang != 'en' and isinstance(response_text, str):
            response_text = translate_text(response_text, input_lang)

        # Store RAG output ONLY if confidence is high
        if retrieval_confidence >= config.rag.min_retrieval_confidence:
            # response_output = response["response"]
            response_output = AIMessage(content=response_text)
        else:
            response_output = AIMessage(content="")
        
        return {
            **state,
            "output": response_output,
            "needs_human_validation": False,  # Assuming no validation needed for RAG responses
            "retrieval_confidence": retrieval_confidence,
            "agent_name": "RAG_AGENT",
            "insufficient_info": insufficient_info
        }

    # Web Search Processor Node
    def run_web_search_processor_agent(state: AgentState) -> AgentState:
        """Handles web search results, processes them with LLM, and generates a refined response."""

        print(f"Selected agent: WEB_SEARCH_PROCESSOR_AGENT")
        print("[WEB_SEARCH_PROCESSOR_AGENT] Processing Web Search Results...")
        
        messages = state["messages"]
        web_search_context_limit = config.web_search.context_limit

        # Get input language from state
        input_lang = state.get("input_lang", "vi")

        recent_context = ""
        for msg in messages[-web_search_context_limit:]: # limit controlled from config
            if isinstance(msg, HumanMessage):
                # print("######### DEBUG 1:", msg)
                recent_context += f"User: {msg.content}\n"
            elif isinstance(msg, AIMessage):
                # print("######### DEBUG 2:", msg)
                recent_context += f"Assistant: {msg.content}\n"

        web_search_processor = WebSearchProcessorAgent(config)

        processed_response = web_search_processor.process_web_search_results(query=state["current_input"], chat_history=recent_context)

        # Translate response if needed
        if input_lang != 'en':
            processed_response.content = translate_text(processed_response.content, input_lang)
        
        if state['agent_name'] != None:
            involved_agents = f"{state['agent_name']}, WEB_SEARCH_PROCESSOR_AGENT"
        else:
            involved_agents = "WEB_SEARCH_PROCESSOR_AGENT"

        # Overwrite any previous output with the processed Web Search response
        return {
            **state,
            # "output": "This would be handled by the web search agent, finding the latest information.",
            "output": processed_response,
            "agent_name": involved_agents
        }

    # Define Routing Logic
    def confidence_based_routing(state: AgentState) -> Dict[str, str]:
        """Route based on RAG confidence score and response content."""
        # Debug prints
        print(f"Routing check - Retrieval confidence: {state.get('retrieval_confidence', 0.0)}")
        print(f"Routing check - Insufficient info flag: {state.get('insufficient_info', False)}")
        
        # Redirect if confidence is low or if response indicates insufficient info
        if (state.get("retrieval_confidence", 0.0) < config.rag.min_retrieval_confidence or 
            state.get("insufficient_info", False)):
            print("Re-routed to Web Search Agent due to low confidence or insufficient information...")
            return "WEB_SEARCH_PROCESSOR_AGENT"  # Correct format
        return "check_validation"  # No transition needed if confidence is high and info is sufficient
    
    def run_brain_tumor_agent(state: AgentState) -> AgentState:
        """Handle brain MRI image analysis."""

        print(f"Selected agent: BRAIN_TUMOR_AGENT")

        current_input = state["current_input"]
        image_path = current_input.get("image", None)

        # Get input language from state
        input_lang = state.get("input_lang", "vi")

        if not image_path:
            response_text = "No image was provided for analysis. Please upload a brain MRI image."
            if input_lang != 'en':
                response_text = translate_text(response_text, input_lang)
            response = AIMessage(content=response_text)
            return {
                **state,
                "output": response,
                "needs_human_validation": False,
                "agent_name": "BRAIN_TUMOR_AGENT"
            }

        try:
            # Initialize the brain tumor analyzer
            from agents.image_analysis_agent.brain_tumor_agent.brain_tumor_inference import BrainTumorInference
            brain_tumor_analyzer = BrainTumorInference()

            # Analyze the MRI image
            analysis_results = brain_tumor_analyzer.analyze_mri(image_path)

            # Check if there was an error in analysis
            if 'error' in analysis_results:
                response_text = f"Error analyzing the image: {analysis_results['error']}"
                if input_lang != 'en':
                    response_text = translate_text(response_text, input_lang)
                response = AIMessage(content=response_text)
                return {
                    **state,
                    "output": response,
                    "needs_human_validation": False,
                    "agent_name": "BRAIN_TUMOR_AGENT"
                }

            # Format the response based on analysis results
            confidence = analysis_results['confidence'] * 100
            
            if not analysis_results['has_tumor']:
                response_text = f"""Brain MRI Analysis Results:

🔍 **Tumor Detection**: NEGATIVE
📊 **Confidence**: {confidence:.1f}%
📝 **Recommendation**: {analysis_results['recommendation']}

ℹ️ **Note**: While no tumor was detected, regular medical check-ups are still recommended."""
            else:
                tumor_type = analysis_results['tumor_type']
                response_text = f"""Brain MRI Analysis Results:

🔍 **Tumor Detection**: POSITIVE
📋 **Tumor Type**: {tumor_type}
📊 **Confidence**: {confidence:.1f}%

📈 **Detailed Probabilities**:
{chr(10).join([f"- {class_name}: {prob*100:.1f}%" for class_name, prob in analysis_results['class_probabilities'].items()])}

📝 **Recommendation**: {analysis_results['recommendation']}

⚠️ **Important Note**: This is an AI-assisted analysis. Please consult with a medical professional for proper diagnosis and treatment."""

            # Translate the response if needed
            if input_lang != 'en':
                response_text = translate_text(response_text, input_lang)

            response = AIMessage(content=response_text)

            return {
                **state,
                "output": response,
                "needs_human_validation": True,  # Medical diagnosis always needs validation
                "agent_name": "BRAIN_TUMOR_AGENT"
            }

        except Exception as e:
            error_text = f"An error occurred while analyzing the brain MRI image: {str(e)}"
            if input_lang != 'en':
                error_text = translate_text(error_text, input_lang)
            error_response = AIMessage(content=error_text)
            return {
                **state,
                "output": error_response,
                "needs_human_validation": False,
                "agent_name": "BRAIN_TUMOR_AGENT"
            }
    
    def run_chest_xray_agent(state: AgentState) -> AgentState:
        """Handle chest X-ray image analysis."""

        current_input = state["current_input"]
        image_path = current_input.get("image", None)

        print(f"Selected agent: CHEST_XRAY_AGENT")

        # Get input language from state
        input_lang = state.get("input_lang", "vi")

        # classify chest x-ray into covid or normal
        predicted_class = AgentConfig.image_analyzer.classify_chest_xray(image_path)

        if predicted_class == "covid19":
            response_text = "The analysis of the uploaded chest X-ray image indicates a **POSITIVE** result for **COVID-19**."
        elif predicted_class == "normal":
            response_text = "The analysis of the uploaded chest X-ray image indicates a **NEGATIVE** result for **COVID-19**, i.e., **NORMAL**."
        else:
            response_text = "The uploaded image is not clear enough to make a diagnosis / the image is not a medical image."

        # Translate response if needed
        if input_lang != 'en':
            response_text = translate_text(response_text, input_lang)

        response = AIMessage(content=response_text)

        return {
            **state,
            "output": response,
            "needs_human_validation": True,  # Medical diagnosis always needs validation
            "agent_name": "CHEST_XRAY_AGENT"
        }
    
    def run_skin_lesion_agent(state: AgentState) -> AgentState:
        """Handle skin lesion image analysis."""

        current_input = state["current_input"]
        image_path = current_input.get("image", None)

        print(f"Selected agent: SKIN_LESION_AGENT")

        # Get input language from state
        input_lang = state.get("input_lang", "vi")

        # classify chest x-ray into covid or normal
        predicted_mask = AgentConfig.image_analyzer.segment_skin_lesion(image_path)

        if predicted_mask:
            response_text = "Following is the analyzed **segmented** output of the uploaded skin lesion image:"
        else:
            response_text = "The uploaded image is not clear enough to make a diagnosis / the image is not a medical image."

        # Translate response if needed
        if input_lang != 'en':
            response_text = translate_text(response_text, input_lang)

        response = AIMessage(content=response_text)

        return {
            **state,
            "output": response,
            "needs_human_validation": True,  # Medical diagnosis always needs validation
            "agent_name": "SKIN_LESION_AGENT"
        }
    
    def handle_human_validation(state: AgentState) -> Dict:
        """Prepare for human validation if needed."""
        if state.get("needs_human_validation", False):
            return {"agent_state": state, "next": "human_validation", "agent": "HUMAN_VALIDATION"}
        return {"agent_state": state, "next": END}
    
    def perform_human_validation(state: AgentState) -> AgentState:
        """Handle human validation process."""
        print(f"Selected agent: HUMAN_VALIDATION")
        
        # Get input language from state
        input_lang = state.get("input_lang", "vi")
        
        # Get the original output content
        output_content = state['output'].content
        
        # Create the validation prompt
        validation_prompt = f"{output_content}\n\n**Human Validation Required:**\n- If you're a healthcare professional: Please validate the output. Select **Yes** or **No**. If No, provide comments.\n- If you're a patient: Simply click Yes to confirm."
        
        # Translate the validation prompt if needed
        if input_lang != 'en':
            validation_prompt = translate_text(validation_prompt, input_lang)
        
        # Create an AI message with the validation prompt
        validation_message = AIMessage(content=validation_prompt)

        return {
            **state,
            "output": validation_message,
            "agent_name": f"{state['agent_name']}, HUMAN_VALIDATION"
        }

    # Check output through guardrails
    def apply_output_guardrails(state: AgentState) -> AgentState:
        """Apply output guardrails to the generated response."""
        output = state["output"]
        current_input = state["current_input"]

        # Check if output is valid
        if not output or not isinstance(output, (str, AIMessage)):
            return state

        output_text = output if isinstance(output, str) else output.content
        
        # Get input language from state
        input_lang = state.get("input_lang", "vi")
        
        # If the last message was a human validation message
        if "Human Validation Required" in output_text:
            # Check if the current input is a human validation response
            validation_input = ""
            if isinstance(current_input, str):
                validation_input = current_input
            elif isinstance(current_input, dict):
                validation_input = current_input.get("text", "")
            
            # If validation input exists
            if validation_input.lower().startswith(('yes', 'no')):
                # Add the validation result to the conversation history
                validation_response = HumanMessage(content=f"Validation Result: {validation_input}")
                
                # If validation is 'No', modify the output
                if validation_input.lower().startswith('no'):
                    fallback_message_text = "The previous medical analysis requires further review. A healthcare professional has flagged potential inaccuracies."
                    
                    # Translate if needed
                    if input_lang != 'en':
                        fallback_message_text = translate_text(fallback_message_text, input_lang)
                    
                    fallback_message = AIMessage(content=fallback_message_text)
                    return {
                        **state,
                        "messages": [validation_response, fallback_message],
                        "output": fallback_message
                    }
                
                return {
                    **state,
                    "messages": validation_response
                }
        
        # Get the original input text
        input_text = ""
        if isinstance(current_input, str):
            input_text = current_input
        elif isinstance(current_input, dict):
            input_text = current_input.get("text", "")
        
        # Apply output sanitization
        sanitized_output = guardrails.check_output(output_text, input_text)
        
        # Translate the sanitized output if needed
        if input_lang != 'en':
            sanitized_output = translate_text(sanitized_output, input_lang)
        
        # For non-validation cases, add the sanitized output to messages
        sanitized_message = AIMessage(content=sanitized_output) if isinstance(output, AIMessage) else sanitized_output
        
        return {
            **state,
            "messages": sanitized_message,
            "output": sanitized_message
        }

    
    # Create the workflow graph
    workflow = StateGraph(AgentState)
    
    # Add nodes for each step
    workflow.add_node("analyze_input", analyze_input)
    workflow.add_node("route_to_agent", route_to_agent)
    workflow.add_node("CONVERSATION_AGENT", run_conversation_agent)
    workflow.add_node("RAG_AGENT", run_rag_agent)
    workflow.add_node("WEB_SEARCH_PROCESSOR_AGENT", run_web_search_processor_agent)
    workflow.add_node("BRAIN_TUMOR_AGENT", run_brain_tumor_agent)
    workflow.add_node("CHEST_XRAY_AGENT", run_chest_xray_agent)
    workflow.add_node("SKIN_LESION_AGENT", run_skin_lesion_agent)
    workflow.add_node("check_validation", handle_human_validation)
    workflow.add_node("human_validation", perform_human_validation)
    workflow.add_node("apply_guardrails", apply_output_guardrails)
    
    # Define the edges (workflow connections)
    workflow.set_entry_point("analyze_input")
    # workflow.add_edge("analyze_input", "route_to_agent")
    # Add conditional routing for guardrails bypass
    workflow.add_conditional_edges(
        "analyze_input",
        check_if_bypassing,
        {
            "apply_guardrails": "apply_guardrails",
            "route_to_agent": "route_to_agent"
        }
    )
    
    # Connect decision router to agents
    workflow.add_conditional_edges(
        "route_to_agent",
        lambda x: x["next"],
        {
            "CONVERSATION_AGENT": "CONVERSATION_AGENT",
            "RAG_AGENT": "RAG_AGENT",
            "WEB_SEARCH_PROCESSOR_AGENT": "WEB_SEARCH_PROCESSOR_AGENT",
            "BRAIN_TUMOR_AGENT": "BRAIN_TUMOR_AGENT",
            "CHEST_XRAY_AGENT": "CHEST_XRAY_AGENT",
            "SKIN_LESION_AGENT": "SKIN_LESION_AGENT",
            "needs_validation": "RAG_AGENT"  # Default to RAG if confidence is low
        }
    )
    
    # Connect agent outputs to validation check
    workflow.add_edge("CONVERSATION_AGENT", "check_validation")
    # workflow.add_edge("RAG_AGENT", "check_validation")
    workflow.add_edge("WEB_SEARCH_PROCESSOR_AGENT", "check_validation")
    workflow.add_conditional_edges("RAG_AGENT", confidence_based_routing)
    workflow.add_edge("BRAIN_TUMOR_AGENT", "check_validation")
    workflow.add_edge("CHEST_XRAY_AGENT", "check_validation")
    workflow.add_edge("SKIN_LESION_AGENT", "check_validation")

    workflow.add_edge("human_validation", "apply_guardrails")
    workflow.add_edge("apply_guardrails", END)
    
    workflow.add_conditional_edges(
        "check_validation",
        lambda x: x["next"],
        {
            "human_validation": "human_validation",
            END: "apply_guardrails"  # Route to guardrails instead of END
        }
    )
    
    # workflow.add_edge("human_validation", END)
    
    # Compile the graph
    return workflow.compile(checkpointer=memory)


def init_agent_state() -> AgentState:
    """Initialize the agent state with default values."""
    return {
        "messages": [],
        "agent_name": None,
        "current_input": None,
        "has_image": False,
        "image_type": None,
        "output": None,
        "needs_human_validation": False,
        "retrieval_confidence": 0.0,
        "bypass_routing": False,
        "insufficient_info": False,
        "input_lang": "vi"
    }


def detect_language(text: str) -> str:
    """
    Detect the language of the input text using the conversation LLM.
    
    Args:
        text: The text to detect language for
        
    Returns:
        Language code (e.g., 'en', 'vi', etc.)
    """
    detection_prompt = f"""Please detect the language of the following text and respond with only the ISO 639-1 language code (e.g., 'en' for English, 'vi' for Vietnamese, etc.):

    Text: {text}

    Language code:"""

    detected_lang = config.conversation.llm.invoke(detection_prompt)
    return detected_lang.content.strip().lower()

def translate_text(text: str, target_lang: str) -> str:
    """
    Translate text to the target language using the conversation LLM.
    
    Args:
        text: The text to translate
        target_lang: Target language code (e.g., 'en', 'vi')
        
    Returns:
        Translated text
    """
    lang_names = {
        'en': 'English',
        'vi': 'Vietnamese',
        # Add more languages as needed
    }
    
    target_lang_name = lang_names.get(target_lang, target_lang)
    
    translation_prompt = f"""Please translate the following text to {target_lang_name}. 
    You must maintain the exact same formatting as the original text, including:
    - All line breaks and paragraph structures
    - All bullet points, numbering, and lists
    - All special characters and symbols
    - All text emphasis (bold, italic) marked with asterisks or other markdown
    - All headings and subheadings
    - All technical terms and medical terminology
    
    Do not change or omit any information during translation. Keep markdown formatting intact.
    If there are any medical terms that should not be translated, keep them in English.

    Text to translate:
    {text}

    {target_lang_name} translation (maintaining exact formatting):"""

    translated_text = config.conversation.llm.invoke(translation_prompt)
    return translated_text.content

def process_query(query: Union[str, Dict], conversation_history: List[BaseMessage] = None) -> str:
    """
    Process a user query through the agent decision system.
    
    Args:
        query: User input (text string or dict with text and image)
        conversation_history: Optional list of previous messages, NOT NEEDED ANYMORE since the state saves the conversation history now
        
    Returns:
        Response from the appropriate agent
    """
    # Initialize the graph
    graph = create_agent_graph()
    
    # Initialize state
    state = init_agent_state()
    
    # Extract query text and detect language
    if isinstance(query, dict):
        query_text = query.get("text", "")
    else:
        query_text = query

    # Detect input language if there's text, otherwise default to English
    input_lang = 'vi'  # Default to Vietnamese
    if query_text:
        detected_lang = detect_language(query_text)
        # Only use detected language if not empty
        if detected_lang and len(detected_lang) > 0:
            input_lang = detected_lang
        
        # Translate input to English if it's not already English
        if input_lang != 'en':
            translated_query = translate_text(query_text, 'en')
            if isinstance(query, dict):
                query["text"] = translated_query
            else:
                query = translated_query
    
    # Store the original language in the state
    state["input_lang"] = input_lang
    
    # Add the current query (now in English)
    state["current_input"] = query

    # Rewrite the query to be more clear and understandable
    if isinstance(query, dict):
        query_text = query.get("text", "")
    else:
        query_text = query

    # Only rewrite if there's actual text to rewrite
    if query_text:
        rewrite_prompt = f"""Please rewrite the following query in clear, simple English while maintaining its original meaning and intent:

        Original query: {query_text}

        Rewritten query:"""

        rewritten_query = config.conversation.llm.invoke(rewrite_prompt)
        
        # Update the query with the rewritten version
        if isinstance(query, dict):
            query["text"] = rewritten_query.content
        else:
            query = rewritten_query.content
            
        # Update state with rewritten query
        state["current_input"] = query

    # To handle image upload case
    if isinstance(query, dict):
        query = query.get("text", "") + ", user uploaded an image for diagnosis."
    
    state["messages"] = [HumanMessage(content=query)]

    # Process the query
    result = graph.invoke(state, thread_config)

    # Keep history to reasonable size
    if len(result["messages"]) > config.max_conversation_history:
        result["messages"] = result["messages"][-config.max_conversation_history:]

    # Always translate messages to user's input language unless the detected language was explicitly English
    if input_lang != 'en':
        translated_messages = []
        for message in result["messages"]:
            if isinstance(message, (HumanMessage, AIMessage)):
                translated_content = translate_text(message.content, input_lang)
                translated_messages.append(type(message)(content=translated_content))
            else:
                translated_messages.append(message)
        result["messages"] = translated_messages

    # visualize conversation history in console
    for m in result["messages"]:
        m.pretty_print()
    
    # Add the response to conversation history
    return result