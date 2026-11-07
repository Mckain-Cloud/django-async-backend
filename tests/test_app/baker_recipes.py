from model_bakery.recipe import (
    Recipe,
    seq,
)

from .models import SelectRelatedAuthorModel

author = Recipe(SelectRelatedAuthorModel, name=seq("named author "))
