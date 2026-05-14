def user_role(request):
    if request.user.is_authenticated:
        profile = getattr(request.user, 'profile', None)
        return {
            'is_manager': profile.is_manager if profile else False,
            'is_provider': profile.is_provider if profile else False,
        }
    return {'is_manager': False, 'is_provider': False}
