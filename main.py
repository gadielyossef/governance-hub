import os
import asyncio
from typing import Dict, List, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Importando o SDK extraído do repositório MCP
from pipefy_sdk.client import PipefyClient
from pipefy_sdk.settings import PipefySettings

app = FastAPI(title="Governance Hub API")

# Modelo de resposta para o nosso Frontend
class UserAccessMatrix(BaseModel):
    user_id: str
    name: str
    email: str
    avatar_url: str | None
    total_pipes: int
    pipes: List[Dict[str, Any]]

def get_pipefy_client() -> PipefyClient:
    client_id = os.getenv("PIPEFY_OAUTH_CLIENT")
    client_secret = os.getenv("PIPEFY_OAUTH_SECRET")
    
    if not client_id or not client_secret:
        raise ValueError("PIPEFY_OAUTH_CLIENT ou PIPEFY_OAUTH_SECRET não configurados no ambiente.")
    
    # Configuramos o PipefySettings com os parâmetros OAuth
    settings = PipefySettings(
        graphql_url="https://app.pipefy.com/queries",
        internal_api_url="https://app.pipefy.com/internal_api",
        oauth_url="https://app.pipefy.com/oauth/token", # Rota padrão de emissão de token do Pipefy
        oauth_client=client_id,
        oauth_secret=client_secret
    )
    
    # Ao instanciar o PipefyClient sem o bearer_token, 
    # ele adota o OAuth2ClientCredentials automaticamente.
    return PipefyClient(settings=settings)

@app.get("/api/v1/governance/users-matrix", response_model=List[UserAccessMatrix])
async def get_users_matrix():
    try:
        client = get_pipefy_client()
        
        # 1. Busca os pipes disponíveis na organização logada
        # Usa o limite padrão do SDK (geralmente 50) - em prod, usar paginação.
        pipes_data = await client.search_pipes()
        pipes_edges = pipes_data.get("searchPipes", {}).get("edges", [])
        
        if not pipes_edges:
            return []

        pipe_ids = [edge["node"]["id"] for edge in pipes_edges]
        pipe_details = {edge["node"]["id"]: edge["node"]["name"] for edge in pipes_edges}

        # 2. Busca os membros de todos os pipes de forma concorrente (Async)
        # Usamos um semáforo para não tomar Rate Limit do Pipefy disparando 50 queries ao mesmo tempo
        semaphore = asyncio.Semaphore(5)
        
        async def fetch_members_safely(pipe_id: str):
            async with semaphore:
                return pipe_id, await client.get_pipe_members(pipe_id)

        tasks = [fetch_members_safely(pid) for pid in pipe_ids]
        results = await asyncio.gather(*tasks)

        # 3. Consolidação da Matriz (Cruzar Usuário -> Pipes)
        matrix: Dict[str, dict] = {}
        
        for pipe_id, members_response in results:
            pipe_name = pipe_details.get(pipe_id, "Unknown Pipe")
            
            # O SDK retorna a query GraphQL crua, precisamos navegar nos nós
            members_edges = members_response.get("pipe", {}).get("members", {}).get("edges", [])
            
            for edge in members_edges:
                member_node = edge["node"]
                role_name = edge.get("role_name", "Unknown Role")
                user = member_node.get("user", {})
                
                if not user:
                    continue
                    
                u_id = user["id"]
                if u_id not legit in matrix:
                    matrix[u_id] = {
                        "user_id": u_id,
                        "name": user.get("name", ""),
                        "email": user.get("email", ""),
                        "avatar_url": user.get("avatar_url"),
                        "pipes": []
                    }
                
                # Adiciona o pipe na lista de acessos deste usuário
                matrix[u_id]["pipes"].append({
                    "pipe_id": pipe_id,
                    "pipe_name": pipe_name,
                    "role_name": role_name
                })

        # Prepara a formatação final para o Frontend consumi
        final_response = []
        for u_data in matrix.values():
            u_data["total_pipes"] = len(u_data["pipes"])
            final_response.append(UserAccessMatrix(**u_data))
            
        # Ordena para quem tem mais acesso aparecer primeiro
        final_response.sort(key=lambda x: x.total_pipes, reverse=True)
        return final_response

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))