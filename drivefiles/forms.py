from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User


class AgentSignupForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("username",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["username"].widget.attrs.update(
            {
                "autocomplete": "username",
                "placeholder": "Choose an agent username",
            }
        )
        self.fields["username"].help_text = ""
        self.fields["password1"].widget.attrs.update(
            {
                "autocomplete": "new-password",
                "placeholder": "Create a strong password",
            }
        )
        self.fields["password1"].help_text = ""
        self.fields["password2"].widget.attrs.update(
            {
                "autocomplete": "new-password",
                "placeholder": "Confirm your password",
            }
        )
        self.fields["password2"].help_text = ""
