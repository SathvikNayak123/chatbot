from langchain_community.document_loaders import PyPDFLoader, DirectoryLoader
from langchain_ollama import OllamaLLM
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain.chains import create_history_aware_retriever, create_retrieval_chain
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_community.utilities import WikipediaAPIWrapper
from langchain_community.tools import WikipediaQueryRun
from RAG.agent import RouteQuery, GraphState
from langchain_groq import ChatGroq
from langgraph.graph import END, StateGraph, START

class FinancialAdvisorBot:
    def __init__(self):
        # Initialize the LLaMA model
        self.llm = OllamaLLM(model="hf.co/sathvik123/llama3-ChatDoc")

        # for agent
        groq_key = "SET_GROQ_KEY"
        router_llm=ChatGroq(groq_api_key=groq_key, model_name="Gemma2-9b-It")
        self.llm_with_tool = router_llm.with_structured_output(RouteQuery)

        api_wrapper=WikipediaAPIWrapper(top_k_results=1,doc_content_chars_max=1000)
        self.wiki_query=WikipediaQueryRun(api_wrapper=api_wrapper)

        # set up embeddings
        self.embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")

        # set up chroma db
        # self.populate_chroma() 

        # data retriever
        self.retriever = Chroma(
            persist_directory="RAG/chroma",
            embedding_function=self.embeddings
        ).as_retriever(
            search_type="similarity", 
            k=3
        )

        self.store = {} # stores chat history
        
        self.chatbot = self.get_chatbot()
        self.chain = self.router_agent()
        self.app = self.build_dag()

    def populate_chroma(self):

        #Extract Data From the PDF File
        loader= DirectoryLoader('RAG/data',
                            glob="*.pdf",
                            loader_cls=PyPDFLoader)

        documents=loader.load()

        #Split the Data into Text Chunks
        text_splitter=RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        text_chunks=text_splitter.split_documents(documents)

        Chroma.from_documents(text_chunks, embedding=self.embeddings, persist_directory='RAG/chroma')

    def get_session_history(self, session_id: str) -> BaseChatMessageHistory:
        if session_id not in self.store:
            self.store[session_id] = ChatMessageHistory()
        return self.store[session_id]

    def get_chatbot(self):

        contextualize_sys_prompt = """Given a chat history and the latest user question \
        which might reference context in the chat history, formulate a standalone question \
        which can be understood without the chat history. Do NOT answer the question, \
        just reformulate it if needed and otherwise return it as is."""

        self.contextualized_q_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", contextualize_sys_prompt),
                MessagesPlaceholder("chat_history"),
                ("human", "{input}"),
            ]
        )

        self.history_aware_retriever = create_history_aware_retriever(self.llm, self.retriever, self.contextualized_q_prompt)

        qa_sys_prompt = """You are a medical professional. \
        Use the following pieces of retrieved context to answer the question. \
        It should consist of paragraph and conversational aspect rather than just a summary. \
        If you don't know the answer, just say that you don't know. \

        {context}"""

        self.qa_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", qa_sys_prompt),
                MessagesPlaceholder("chat_history"),
                ("human", "{input}"),
            ]
        )

        self.qa_chain=create_stuff_documents_chain(self.llm, self.qa_prompt)
        self.rag_chain=create_retrieval_chain(self.history_aware_retriever, self.qa_chain)

        chatbot = RunnableWithMessageHistory(
            self.rag_chain,
            self.get_session_history,
            input_messages_key="input",
            history_messages_key="chat_history",
            output_messages_key="answer",
        )

        return chatbot

    def router_agent(self):

        system = """You are an expert at routing a user question to a vectorstore or wikipedia.
        The vectorstore contains documents related to medical symptoms and diagnosis.
        Use the vectorstore for questions on these topics. Otherwise, use wiki-search."""

        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", system),
                ("human", "{question}"),
            ]
        )

        chain = prompt | self.llm_with_tool
        return chain
    
    def retrieve(self, state):
        """
        retrieval from vector store
        """
        question = state["question"]

        documents = self.chatbot.invoke(
                        {"input": question},
                        config={"configurable": {"session_id": "01"}
                        }, 
                    )["answer"]
        return {"documents": documents, "question": question}

    def web_search(self, state):
        """
        wiki search based on the re-phrased question.
        """
        question = state["question"]

        # Wiki search
        docs = self.wiki_query.invoke({"query": question})

        return {"documents": docs, "question": question}
    
    def route_question(self, state):
        """
        Route question to wiki search or RAG.
        """
        question = state["question"]
        source = self.chain.invoke({"question": question})
        
        if source.datasource == "wiki_search":
            print("---ROUTE QUESTION TO Wiki SEARCH---")
            return "wiki_search"
        elif source.datasource == "vectorstore":
            print("---ROUTE QUESTION TO RAG---")
            return "vectorstore"
    
    def build_dag(self):
        workflow = StateGraph(GraphState)

        # Define the nodes
        workflow.add_node("web_search", self.web_search)  # web search
        workflow.add_node("retrieve", self.retrieve)  # retrieve

        # Build graph
        workflow.add_conditional_edges(
            START,
            self.route_question,
            {
                "wiki_search": "web_search",
                "vectorstore": "retrieve",
            },
        )
        workflow.add_edge( "retrieve", END)
        workflow.add_edge( "web_search", END)

        # Compile
        return workflow.compile()

    async def get_response(self, user_query: str):
        """Asynchronous method for processing user query through the chatbot."""

        inputs = {
            "question": user_query
        }
        for output in self.app.stream(inputs):
            for key, value in output.items():
                response = value["documents"]

        return response