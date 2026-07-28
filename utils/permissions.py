from __future__ import annotations

import discord

import config


def has_anime_permission(interaction: discord.Interaction) -> bool:
    """Permite administradores ou usuários com cargos definidos em config.ANIME_ALLOWED_ROLES."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False

    if interaction.user.guild_permissions.administrator:
        return True

    allowed_roles = set(getattr(config, "ANIME_ALLOWED_ROLES", []))
    user_role_ids = {role.id for role in interaction.user.roles}
    return bool(allowed_roles.intersection(user_role_ids))
