import deepchem as dc 
import numpy as np
import tensorflow as tf
import pandas as pd

import inspect
import itertools
import logging
import os
import random
import tempfile
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union
#from feat import RDKitDescriptors
import deepchem as dc
import numpy as np
import pandas as pd
from splitters import SingletaskStratifiedSplitter
from deepchem.data import Dataset, DiskDataset
from deepchem.utils import get_print_threshold
import inspect 

smiles = ['CC(=O)OC1=CC=CC=C1C(=O)O']
featurizer = dc.feat.RDKitDescriptors(descriptors = ['NumHeteroatoms', 'HeavyAtomMolWt', 'ExactMolWt', 'NumValenceElectrons'])
features = featurizer.featurize(smiles)
print(inspect.getfullargspec(dc.feat.RDKitDescriptors).args)
print(features)
print("DeepChem version:", dc.__version__)