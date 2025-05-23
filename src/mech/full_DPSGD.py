from functools import partial
import logging
import secrets
import multiprocessing
import os
import sys
import argparse
import ast
import numpy as np
from utils.utils import DUMMY_CONSTANT, _ensure_2dim
from numpy.random import MT19937, RandomState
import copy

import torch
import torch.nn as nn
import torch.optim as optim
import torch.utils.data
import torchvision.transforms as transforms
import torch.nn.functional as torch_F
from opacus import PrivacyEngine
from torchvision.datasets import CIFAR10
from tqdm import tqdm
from tqdm import tqdm

from collections import OrderedDict

from estimator.basic import _GeneralNaiveEstimator
from estimator.ptlr import _PTLREstimator
from auditor.basic import _GeneralNaiveAuditor
from analysis.tradeoff_Gaussian import Gaussian_curve, find_eps, find_optimal_mu

import gc
torch.set_num_threads(1)

from .model_architecture import MODEL_MAPPING

def train(model, train_loader, optimizer, device):
    model.train()
    criterion = nn.CrossEntropyLoss()
    for images, target in tqdm(train_loader):
        images, target = images.to(device), target.to(device)
        optimizer.zero_grad()
        output = model(images)
        loss = criterion(output, target)
        loss.backward()
        optimizer.step()
        
        # Clear memory after each batch
        del images, target, output, loss
    
    # Final cleanup after training
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()

def evaluate_loss(model, dataloader, device):
    model.eval()
    loss_fn = nn.CrossEntropyLoss()
    total_loss = 0.0
    total_samples = 0
    with torch.no_grad():
        for X, y in dataloader:
            X, y = X.to(device), y.to(device)
            logits = model(X)
            loss = loss_fn(logits, y)
            total_loss += loss.item() * X.size(0)
            total_samples += X.size(0)
            
            # Clear memory after each batch
            del X, y, logits, loss
    
    # Final cleanup after evaluation
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()
    
    return total_loss / total_samples

def generate_params(
    data_args,
    log_dir=None,
    batch_size=512,
    epochs=1,
    lr=0.1,
    sigma=1.0,
    max_grad_norm=1.0,
    device="cpu",
    auditing_approach="1d_logit",
    model_name="convnet_balanced",
    num_samples=1000,
    num_train_samples=1000,
    num_test_samples=1000,
    h=0.01,
    eta_max=15,
    database_size=50000,
    gamma=0.05,
    claimed_f=None,
    intermediate_epoch_list=[]
):
    # This function generates parameters for training and dataset setup, including
    # configurations for the SGD algorithm and neighboring datasets for differential privacy.

    if "method" not in data_args or "data_dir" not in data_args or "internal_result_path" not in data_args:
        raise ValueError(f"Neighboring database generation is not properly defined")
    
    if not os.path.exists(data_args["internal_result_path"]):
        os.makedirs(data_args["internal_result_path"])

    # Set up neighboring datasets for differential privacy
    if data_args["method"] == "default":
        data_dir = data_args["data_dir"]
        transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
        ])
        x1 = CIFAR10(root=data_dir, train=True, download=True, transform=transform)
        x1.data[0] = get_white_image()
        x1.targets[0] = 0  # arbitrary label (e.g., class "airplane")

        x0 = copy.deepcopy(x1)
        x0.data[0] = get_black_image()
        x0.targets[0] = 0  # arbitrary label (e.g., class "airplane")

        if database_size < len(x1):
            x1 = torch.utils.data.Subset(x1, range(database_size))
            x0 = torch.utils.data.Subset(x0, range(database_size))
    else:
        raise ValueError(f"Neighboring database generation method is undefined")
    
    if log_dir is not None:
        log_file_path = os.path.join(log_dir, "sgd-test.log")
    else:
        log_file_path = None

    if auditing_approach not in ["1d_logit", "1d_cross_entropy", "1d_KLDivLoss"]:
        raise ValueError(f"Auditing approach is undefined")

    if claimed_f is None:
        claimed_f = partial(Gaussian_curve, mean_difference=1.0)
    
    if len(intermediate_epoch_list) == 0:
        intermediate_epoch_list = [epochs]
    
    kwargs = {
        "sgd_alg":{
            "batch_size": batch_size,
            "epochs": epochs,
            "intermediate_epoch_list": intermediate_epoch_list,
            "lr": lr,
            "sigma": sigma,
            "max_grad_norm": max_grad_norm,
            "device": device,
            "internal_result_path": data_args["internal_result_path"],
            "model_name": model_name,
            "database_size": database_size
        },
        "dataset":{
            "x0": x0,
            "x1": x1
        },
        "log_file_path": log_file_path,
        "auditing_approach": auditing_approach,
        "num_samples": num_samples,
        "num_train_samples" : num_train_samples,
        "num_test_samples" : num_test_samples,
        "h": h,
        "eta_max" : eta_max,
        "gamma" : gamma,
        "claimed_f" : claimed_f
    }
    return kwargs

# --- Create full black image (CIFAR-10 format) ---
def get_black_image(tensor_image = False):
    if tensor_image:
        image = torch.zeros((1, 3, 32, 32), dtype=torch.float32)
        # Normalize as per CIFAR-10 normalization used in training
        mean = torch.tensor([0.4914, 0.4822, 0.4465]).view(1, 3, 1, 1)
        std = torch.tensor([0.2023, 0.1994, 0.2010]).view(1, 3, 1, 1)
        image = (image - mean) / std
    else:
        image = np.zeros((32, 32, 3), dtype=np.uint8)
    return image

def get_white_image(tensor_image = False):
    if tensor_image:
        image = torch.ones((1, 3, 32, 32), dtype=torch.float32)  # full white = 1.0 after ToTensor
        # Normalize as per CIFAR-10 normalization used in training
        mean = torch.tensor([0.4914, 0.4822, 0.4465]).view(1, 3, 1, 1)
        std = torch.tensor([0.2023, 0.1994, 0.2010]).view(1, 3, 1, 1)
        image = (image - mean) / std
    else:
        image = np.full((32, 32, 3), 255, dtype=np.uint8)
    return image

def _train_model_worker(args):
    """Worker function for parallel model training."""
    sampler_kwargs, positive = args
    torch.set_num_threads(1)
    sampler = DPSGDSampler(sampler_kwargs)
    model, model_path = sampler.train_model(positive=positive)
    
    # Explicitly clear model from memory
    del model
    torch.cuda.empty_cache() if torch.cuda.is_available() else None
    
    # Force garbage collection
    gc.collect()
    
    return model_path

def _generate_sample_worker(args):
    sampler_kwargs, model_path_list = args
    torch.set_num_threads(1)
    sampler = DPSGDSampler(sampler_kwargs)
    results = []

    if sampler.auditing_approach_name not in ["1d_logit", "1d_cross_entropy", "1d_KLDivLoss"]:
        raise ValueError(f"Auditing approach is undefined")

    for model_path in model_path_list:
        model = sampler.load_model(model_path)
        score = sampler.auditing_approach(model)
        results.append(score)
        
        # Clear model after each iteration
        del model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()
    
    # Final cleanup
    del sampler
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()
    
    return results
    
def parallel_generate_samples(sampler_kwargs, model_paths, num_workers=1, batch_size=50):
    """Load models in parallel, each on a single CPU thread."""
    print(f"\nLoading and projecting models with {num_workers} workers:")
    
    # Ensure we're using spawn method
    multiprocessing.set_start_method('spawn', force=True)

    ctx = multiprocessing.get_context('spawn')
    with ctx.Pool(processes=num_workers) as pool:
        args_list = [
            (sampler_kwargs, model_paths[i:i+batch_size])
            for i in range(0, len(model_paths), batch_size)
        ]

        scores = list(tqdm(
            pool.imap(_generate_sample_worker, args_list),
            total=len(args_list),
            desc="Generating samples"
        ))
    # Flatten the list of lists
    results = []
    for batch in scores:
        results.extend(batch)

    return results

def parallel_train_models(sampler_kwargs, num_generating_samples, num_workers=1):
    """Train num_generating_samples models in parallel, each on a single CPU thread."""
    # Ensure we're using spawn method
    multiprocessing.set_start_method('spawn', force=True)
    
    ctx = multiprocessing.get_context('spawn')
    with ctx.Pool(processes=num_workers) as pool:
        # Combine both positive and negative args into one list
        args_list = [(sampler_kwargs, False)] * num_generating_samples + \
                   [(sampler_kwargs, True)] * num_generating_samples
        
        # Process all models in parallel with progress tracking
        all_results = list(tqdm(
            pool.imap(_train_model_worker, args_list),
            total=len(args_list),
            desc="Training models"
        ))
        
        # Split results back into positive and negative
        negative_models_paths = all_results[:num_generating_samples]
        positive_models_paths = all_results[num_generating_samples:]
    
    return negative_models_paths, positive_models_paths


class DPSGDSampler:
    """
        The sampler takes as inputs a pair of database (x0, x1)
        The parameters of the opacus DPSGD algorithm: initial model theta_0; number of epochs T; learning rate eta; and the noise parameter sigma
        It preprocess and generate N data samples from both distribution SGD(x0) and SGD(x1). 
        It generate a sample set with given eta from the preprocessed dataset
    """
    def __init__(self, kwargs):
        self.kwargs = kwargs

        # Set up dataset
        self.x0 = kwargs["dataset"]["x0"]
        self.x1 = kwargs["dataset"]["x1"]
        
        # Set up SGD algorithm
        self.batch_size = kwargs["sgd_alg"]["batch_size"]
        self.epochs = kwargs["sgd_alg"]["epochs"]
        assert isinstance(self.epochs, int) and self.epochs > 0, "epochs must be a positive integer"
        self.lr = kwargs["sgd_alg"]["lr"]
        self.sigma = kwargs["sgd_alg"]["sigma"]
        self.max_grad_norm = kwargs["sgd_alg"]["max_grad_norm"]
        self.device = kwargs["sgd_alg"]["device"]
        self.database_size = kwargs["sgd_alg"]["database_size"]

        self.model_name = kwargs["sgd_alg"]["model_name"]
        if self.model_name not in MODEL_MAPPING:
            raise ValueError(f"Unsupported model type: {self.model_name}. Supported types are: {list(MODEL_MAPPING.keys())}")
        self.model_class = MODEL_MAPPING[self.model_name]


        # Set up Randomness
        self.bot = -DUMMY_CONSTANT
        self.reset_randomness()

        # Set up auditing approach
        self.auditing_approach_name = kwargs["auditing_approach"]
        if self.auditing_approach_name == "kd" or self.auditing_approach_name == "full":
            raise ValueError(f"Auditing approach has not been implemented yet")     
        
        if self.auditing_approach_name == "1d_logit":
            self.auditing_approach = self.project_model_to_one_dim_logit
            self.dim = 1
        elif self.auditing_approach_name == "1d_cross_entropy":
            self.auditing_approach = self.project_model_to_one_dim_cross_entropy
            self.dim = 1
        elif self.auditing_approach_name == "1d_KLDivLoss":
            self.auditing_approach = self.project_model_to_one_dim_KLDivLoss
            self.dim = 1
        
        if self.model_name in ["convnet", "convnet_balanced"]:
            self.dim_reduction_image = get_black_image(tensor_image=True)
        elif self.model_name in ["resnet20"]:
            self.dim_reduction_image = get_white_image(tensor_image=True)
        
        
        # Set up internal result path
        self.internal_result_path_prefix = kwargs["sgd_alg"]["internal_result_path"]

        self.intermediate_epoch_list = kwargs["sgd_alg"]["intermediate_epoch_list"]
        assert isinstance(self.intermediate_epoch_list, list) and all(isinstance(epoch, int) and epoch > 0 for epoch in self.intermediate_epoch_list), "intermediate_epoch_list must be a list of positive integers"
        if self.epochs not in self.intermediate_epoch_list:
            self.intermediate_epoch_list.append(self.epochs)
            self.intermediate_epoch_list.sort()
        self.intermediate_epoch_list = np.array(self.intermediate_epoch_list)
        
        self.model_folder_mapping = {}
        self.samples_folder_mapping = {}
        for intermediate_epoch in self.intermediate_epoch_list:
            internal_result_path = os.path.join(self.internal_result_path_prefix, self.model_name+'_'+str(self.database_size)+'_'+str(intermediate_epoch))
            self.model_folder_mapping[intermediate_epoch] = os.path.join(internal_result_path, "model_folder")
            self.samples_folder_mapping[intermediate_epoch] = os.path.join(internal_result_path, "samples_folder")
            os.makedirs(self.model_folder_mapping[intermediate_epoch], exist_ok=True)
            os.makedirs(self.samples_folder_mapping[intermediate_epoch], exist_ok=True)

        # Log the information about the CNN sampler
        self.logger = self.get_logger(kwargs["log_file_path"])
        self.logger.info("Initialized %s_DPSGDSampler with parameters: batch_size=%d, epochs=%d, lr=%.2f, sigma=%.2f, max_grad_norm=%.2f, device=%s", 
                    self.model_name, self.batch_size, self.epochs, self.lr, self.sigma, self.max_grad_norm, self.device)
        

    
    def reset_randomness(self):
        seed = secrets.randbits(128)
        self.rng = RandomState(MT19937(seed))
        torch.manual_seed(self.rng.randint(0, 2**32 - 1))
    
    def get_logger(self, file_path=None):
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.INFO)

        
        # Create formatter
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - [PID:%(process)d] - %(message)s')
        
        # Console handler - only show CRITICAL
        ch = logging.StreamHandler()
        ch.setLevel(logging.CRITICAL)
        ch.setFormatter(formatter)
        logger.addHandler(ch)
        
        # File handler - show DEBUG and above
        if file_path:
            fh = logging.FileHandler(file_path)
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(formatter)
            logger.addHandler(fh)
        
        return logger

    def load_model(self, model_path):
        device = torch.device(self.device)   
        # Create model with same architecture
        model = self.model_class(num_classes=10).to(device)
        
        # Load the saved state dict
        state_dict = torch.load(model_path, map_location=device)
        
        # Fix state dict keys if they have _module prefix
        new_state_dict = OrderedDict()
        for k, v in state_dict.items():
            if k.startswith('_module.'):
                name = k[8:]  # remove '_module.'
            else:
                name = k
            new_state_dict[name] = v
        
        # Load the fixed state dict
        model.load_state_dict(new_state_dict)

        self.logger.debug(f"Model loaded from {model_path}")
        
        return model
    
    def evaluate_loss(self, model):
        # Evaluate final loss
        eval_loader = torch.utils.data.DataLoader(self.x0, batch_size=self.batch_size, shuffle=False)
        final_loss = evaluate_loss(model, eval_loader, self.device)
        self.logger.info(f"Final loss on train set (x0): {final_loss:.4f}")

        return final_loss

    def project_model_to_one_dim_logit(self, model):
        # This method projects the model's output to a one-dimensional value, allowing the use of kernel density estimation techniques.
        # The projection is done by taking the softmax output of the model 
        model.eval()
        logits = model(self.dim_reduction_image)
        score = logits.squeeze()[0].item()  # get the first logit value directly without softmax

        return score
    
    def project_model_to_one_dim_cross_entropy(self, model):
        # This method projects the model's output to a one-dimensional value, allowing the use of kernel density estimation techniques.
        # The projection is done by taking the softmax output of the model 
        model.eval()
        logits = model(self.dim_reduction_image)

        target = torch.tensor([0], dtype=torch.long)
        loss = nn.CrossEntropyLoss()(logits, target)
        score = loss.item()

        return score
    
    def project_model_to_one_dim_KLDivLoss(self, model):
        # This method projects the model's output to a one-dimensional value, allowing the use of kernel density estimation techniques.
        # The projection is done by taking the softmax output of the model 
        model.eval()
        logits = model(self.dim_reduction_image)

        # Create one-hot encoded target distribution
        target = torch.zeros_like(logits)
        target[0, 0] = 1.0  # Set probability 1.0 for class 0
        
        log_softmax = torch_F.log_softmax(logits, dim=1)
        loss = torch_F.kl_div(log_softmax, target, reduction='batchmean')
        score = loss.item()

        return score

    def project_flatter(self, model):
        model.eval()
        logits = model(self.dim_reduction_image)
        score = logits.squeeze()[0].item()  # get the first logit value directly without softmax

        return score
    
    def train_model(self, positive=False):  
        # Set up training device
        device = torch.device(self.device)

        # Set up training dataset
        train_dataset = self.x0 if not positive else self.x1
        train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=self.batch_size, shuffle=True)

        # Set up model
        model = self.model_class(num_classes=10).to(device)
        optimizer = optim.SGD(model.parameters(), lr=self.lr, momentum=0.9)

        privacy_engine = PrivacyEngine()
        model, optimizer, train_loader = privacy_engine.make_private(
            module=model,
            optimizer=optimizer,
            data_loader=train_loader,
            noise_multiplier=self.sigma,
            max_grad_norm=self.max_grad_norm,
        )

        # Train model
        for epoch in range(1, self.epochs+1):
            train(model, train_loader, optimizer, device)

            if epoch in self.intermediate_epoch_list:
                # Save model
                run_id = ''.join(f'{self.rng.randint(0, 16):x}' for _ in range(16))
                model_name = f"{self.model_name}_model_x0_{run_id}.pt" if not positive else f"{self.model_name}_model_x1_{run_id}.pt"
                model_path = os.path.join(self.model_folder_mapping[epoch], model_name)
                torch.save(model.state_dict(), model_path)
                self.logger.debug(f"Model saved to {model_path}")
                
                # Clear memory after saving
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                gc.collect()

        # Final cleanup
        del train_loader, optimizer, privacy_engine
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()

        return model, model_path
    
    def preprocess(self, num_samples, num_workers=1, reset = False):
        # First, check if enought samples are already generated
        samples_dir = self.samples_folder_mapping[self.epochs]

        try:
            samples_P = np.loadtxt(os.path.join(samples_dir, f'{self.auditing_approach_name}_d_{self.model_name}.csv'), delimiter=',')
            samples_Q = np.loadtxt(os.path.join(samples_dir, f'{self.auditing_approach_name}_dprime_{self.model_name}.csv'), delimiter=',')
            if reset == False and len(samples_P) >= num_samples and len(samples_Q) >= num_samples:
                self.logger.info(f"Found {num_samples} samples in {samples_dir}. Skipping generation.")
                self.samples_P, self.samples_Q = _ensure_2dim(np.array(samples_P[:num_samples]), np.array(samples_Q[:num_samples]))
                self.computed_samples = num_samples
                return (self.samples_P, self.samples_Q)
            else:
                self.logger.info(f"Need to generate {num_samples} samples.")
        except FileNotFoundError:
            self.logger.info(f"Need to generate {num_samples} samples.")

        if reset == False:
            # Second try to read existing models from the model folder
            existing_negative_models_paths = [os.path.join(self.model_folder_mapping[self.epochs], f) for f in os.listdir(self.model_folder_mapping[self.epochs]) if f.startswith(f"{self.model_name}_model_x0_") and f.endswith(".pt")]
            existing_positive_models_paths = [os.path.join(self.model_folder_mapping[self.epochs], f) for f in os.listdir(self.model_folder_mapping[self.epochs]) if f.startswith(f"{self.model_name}_model_x1_") and f.endswith(".pt")]
            
            # Determine how many models we can load
            num_existing_samples = min(len(existing_negative_models_paths), len(existing_positive_models_paths))
            num_generating_samples = max(0, num_samples - num_existing_samples)
            
            self.logger.info(f"Found {num_existing_samples} existing model pairs. Need to generate {num_generating_samples} more.")
        else:
            existing_negative_models_paths = []
            existing_positive_models_paths = []
            num_existing_samples = 0
            num_generating_samples = num_samples

        generated_negative_models_paths = []
        generated_positive_models_paths = []

        if num_generating_samples > 0:
            self.logger.info(f"Generating {num_generating_samples} additional model pairs")
            generated_negative_models_paths, generated_positive_models_paths = parallel_train_models(self.kwargs, num_generating_samples, num_workers=num_workers)
        
        # Load existing models and compute their projections
        samples_P = []
        samples_Q = []

        negative_models_paths = existing_negative_models_paths[:num_samples] + generated_negative_models_paths
        positive_models_paths = existing_positive_models_paths[:num_samples] + generated_positive_models_paths

        samples_P = parallel_generate_samples(self.kwargs, negative_models_paths, num_workers=num_workers)
        samples_Q = parallel_generate_samples(self.kwargs, positive_models_paths, num_workers=num_workers)            

        self.samples_P, self.samples_Q = _ensure_2dim(np.array(samples_P), np.array(samples_Q))
        self.computed_samples = num_samples

        # Save samples        
        np.savetxt(os.path.join(samples_dir, f'{self.auditing_approach_name}_d_{self.model_name}.csv'), samples_P, delimiter=',')
        np.savetxt(os.path.join(samples_dir, f'{self.auditing_approach_name}_dprime_{self.model_name}.csv'), samples_Q, delimiter=',')
        
        return (self.samples_P, self.samples_Q)
    
    def gen_samples(self, eta, num_samples, reset = False, shuffle = False):
        assert eta > 0
        self.reset_randomness()
        
        if reset == True:
            samples_P, samples_Q = self.preprocess(num_samples, reset = True)
        else:
            if (hasattr(self, "computed_samples") == False) or (self.computed_samples < num_samples):
                samples_P, samples_Q = self.preprocess(num_samples)
                self.computed_samples = num_samples
            else:
                samples_P = self.samples_P[:num_samples]
                samples_Q = self.samples_Q[:num_samples]
                
        if eta > 1:
            p = self.rng.uniform(0, 1, num_samples) > (1.0/eta)
            p = p.reshape((num_samples, 1)) * np.ones((num_samples, self.dim))
            samples_P = (1-p)*samples_P + p*(self.bot*np.ones_like(self.dim))
        if eta < 1:
            p = self.rng.uniform(0, 1, num_samples) > eta
            p = p.reshape((num_samples, 1)) * np.ones((num_samples, self.dim))
            samples_Q = (1-p)*samples_Q + p*(self.bot*np.ones_like(self.dim))
        
        samples = np.vstack((samples_P, samples_Q))
        labels = np.concatenate((np.zeros(num_samples), np.ones(num_samples)))
        
        if shuffle:
            ids = self.rng.permutation(num_samples*2)
            samples = samples[ids]
            labels = labels[ids]
            
        return {'X': samples, 'y': labels}
    

class DPSGD_Estimator(_GeneralNaiveEstimator):
    def __init__(self, kwargs):
        super().__init__(kwargs=kwargs)
        train_kwargs = copy.deepcopy(kwargs)
        train_kwargs["sgd_alg"]["internal_result_path"] = os.path.join(kwargs["sgd_alg"]["internal_result_path"], "train")
        self.train_sampler = DPSGDSampler(train_kwargs)

        test_kwargs = copy.deepcopy(kwargs)
        test_kwargs["sgd_alg"]["internal_result_path"] = os.path.join(kwargs["sgd_alg"]["internal_result_path"], "test")
        self.test_sampler = DPSGDSampler(test_kwargs)

    def build(self, eta, nworkers=1, classifier_args=None):
        output = super().build(eta, nworkers, classifier_args)

        alpha_values = copy.deepcopy(output["alpha"])
        beta_values = copy.deepcopy(output["beta"])

        mu = find_optimal_mu(alpha_values, beta_values)
        self.output_["mu"] = mu

        self.output_["delta"] = 1e-5
        self.output_["eps_lower_bound"] = self.compute_eps_lower_bound(delta = self.output_["delta"])
        
        return self.output_  
    
    def compute_eps_lower_bound(self, delta = 1e-5):
        mu = self.output_["mu"]
        eps = find_eps(mu, delta)
        return eps


class DPSGD_PTLREstimator(_PTLREstimator):
    def __init__(self, kwargs):
        super().__init__(kwargs=kwargs)
        self.sampler = DPSGDSampler(kwargs)
    
    def build(self, eta_max = 15):
        # First call parent class's build function
        output = super().build(eta_max = eta_max)
        
        alpha_values = copy.deepcopy(output["alpha"])
        beta_values = copy.deepcopy(output["beta"])

        mu = find_optimal_mu(alpha_values, beta_values)
        self.output_["mu"] = mu

        self.output_["delta"] = 1e-5
        self.output_["eps_lower_bound"] = self.compute_eps_lower_bound(delta = self.output_["delta"])
        
        return self.output_  
    
    def compute_eps_lower_bound(self, delta = 1e-5):
        mu = self.output_["mu"]
        eps = find_eps(mu, delta)
        return eps


class DPSGD_Auditor(_GeneralNaiveAuditor):
    def __init__(self, kwargs):
        super().__init__(kwargs=kwargs)
        finder_kwargs = copy.deepcopy(kwargs)
        finder_kwargs["sgd_alg"]["internal_result_path"] = os.path.join(kwargs["sgd_alg"]["internal_result_path"], "train")
        self.point_finder = DPSGD_PTLREstimator(finder_kwargs)

        estimator_kwargs = copy.deepcopy(kwargs)
        self.point_estimator = DPSGD_Estimator(estimator_kwargs)