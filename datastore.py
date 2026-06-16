"""
datastore.py - Armazenamento JSON assíncrono, thread-safe e à prova de falhas.

Utiliza asyncio.Lock para sincronização não-bloqueante e aiofiles para I/O
assíncrona. Escrita atômica via arquivo temporário + rename.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import aiofiles
import aiofiles.os


class AsyncJsonStore:
    """
    Armazenamento chave-valor baseado em arquivo JSON único, otimizado para
    ambientes assíncronos (discord.py).
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        """Cria o diretório pai se não existir."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def read(self) -> Dict[str, Any]:
        """
        Lê o arquivo JSON de forma assíncrona.

        Retorna um dicionário vazio se o arquivo não existir ou estiver corrompido.
        Em caso de corrupção, renomeia o arquivo para backup.
        """
        async with self._lock:
            if not self.path.exists():
                return {}

            try:
                async with aiofiles.open(self.path, "r", encoding="utf-8") as f:
                    content = await f.read()
                return json.loads(content)
            except json.JSONDecodeError:
                # Arquivo corrompido: renomeia para debug e retorna vazio
                corrupted_path = self.path.with_suffix(".json.corrupted")
                try:
                    await aiofiles.os.rename(self.path, corrupted_path)
                except Exception:
                    pass
                return {}
            except Exception:
                # Outros erros de I/O: retorna vazio
                return {}

    async def write(self, data: Dict[str, Any]) -> None:
        """
        Escreve os dados no arquivo JSON de forma atômica e assíncrona.

        Usa um arquivo temporário no mesmo diretório e depois o renomeia,
        garantindo que o arquivo nunca fique corrompido ou pela metade.
        """
        async with self._lock:
            self._ensure_dir()

            # Cria arquivo temporário no mesmo diretório (mesmo sistema de arquivos)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".tmp_",
                suffix=".json",
                dir=str(self.path.parent)
            )
            os.close(fd)

            try:
                # Escreve os dados no arquivo temporário
                async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
                    await f.write(json.dumps(data, ensure_ascii=False, indent=2))

                # Renomeia atomicamente (substitui o original)
                await aiofiles.os.replace(tmp_path, str(self.path))

            except Exception:
                # Em caso de erro, tenta remover o temporário
                try:
                    await aiofiles.os.remove(tmp_path)
                except Exception:
                    pass
                raise

    async def update(self, updater: callable) -> None:
        """
        Atalho para ler, modificar e escrever de forma atômica.

        Exemplo:
            await store.update(lambda data: data.setdefault("guilds", {}))
        """
        async with self._lock:
            data = await self.read()
            updater(data)
            await self.write(data)


# Para compatibilidade com código legado que espera um objeto "JsonStore"
# com métodos síncronos (ex.: sistemas não migrados), fornecemos um wrapper.
class SyncJsonStore:
    """Wrapper síncrono para uso em threads (não recomendado para novos códigos)."""

    def __init__(self, path: str):
        self._async_store = AsyncJsonStore(path)

    def read(self) -> Dict[str, Any]:
        """Leitura síncrona (bloqueante). Use apenas em threads separadas."""
        return asyncio.run(self._async_store.read())

    def write(self, data: Dict[str, Any]) -> None:
        """Escrita síncrona (bloqueante). Use apenas em threads separadas."""
        asyncio.run(self._async_store.write(data))