"""`python -m meshprint …` 的進入點(與 console script `meshprint` 等價)。"""
import sys

from .cli import main

sys.exit(main())
