import argparse
import os
from os.path import exists, isdir, join
from typing import Any, Dict, List, Tuple

import bitsandbytes as bnb
import torch
import transformers
from transformers import PreTrainedModel, PreTrainedTokenizer
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR

from chatllms.data.data_utils import (DEFAULT_BOS_TOKEN, DEFAULT_EOS_TOKEN,
                                      DEFAULT_PAD_TOKEN, DEFAULT_UNK_TOKEN)


def add_special_tokens_if_missing(tokenizer: PreTrainedTokenizer,
                                  model: PreTrainedModel):
    """
    If 'llama' or 'baichuan' is in the model name or path, check if the special tokens are set correctly.
    Add any missing special tokens to prevent them from being parsed into different tokens.
    Note that these special tokens are present in the vocabulary.
    Note also that `model.config.pad_token_id` is 0 which corresponds to `<unk>` token.

    Args:
        tokenizer: The pre-trained tokenizer.
        model: The pre-trained model.

    Returns:
        None.
    """
    # Define a dictionary to store any missing special tokens along with their default values
    special_tokens_dict: Dict[str, Any] = {}

    # Check if each special token is present. If not, add it to the special_tokens_dict with its default value.
    if tokenizer.pad_token is None:
        special_tokens_dict['pad_token'] = DEFAULT_PAD_TOKEN
    if tokenizer.eos_token is None:
        special_tokens_dict['eos_token'] = DEFAULT_EOS_TOKEN
    if tokenizer.bos_token is None:
        special_tokens_dict['bos_token'] = DEFAULT_BOS_TOKEN
    if tokenizer.unk_token is None:
        special_tokens_dict['unk_token'] = DEFAULT_UNK_TOKEN

    # If there are any missing special tokens, call `smart_tokenizer_and_embedding_resize()` to add them to the
    # tokenizer and resize the embedding accordingly.
    if len(special_tokens_dict) > 0:
        smart_tokenizer_and_embedding_resize(special_tokens_dict, tokenizer,
                                             model)


def smart_tokenizer_and_embedding_resize(special_tokens_dict: Dict[str, str],
                                         tokenizer: PreTrainedTokenizer,
                                         model: PreTrainedModel) -> None:
    """Resize tokenizer and embedding to accommodate new special tokens.

    Args:
        special_tokens_dict (Dict[str, str]): A dictionary of special tokens to be added to the tokenizer.
        tokenizer (PreTrainedTokenizer): The tokenizer object to be resized.
        model (PreTrainedModel): The model object whose token embeddings are to be resized.

    Returns:
        None

    Note: This function resizes the tokenizer to accommodate additional special tokens and the
    embedding matrix of the model to match the new size of the tokenizer. If any new special tokens
    have been added, the function computes the average embedding values of the existing embeddings
    and sets those values for the new special token embeddings. This is done separately for the input
    embeddings and output embeddings of the model.
    """
    num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)

    # Resize token embeddings to match tokenizer
    model.resize_token_embeddings(len(tokenizer))

    if num_new_tokens > 0:
        input_embeddings = model.get_input_embeddings().weight.data
        output_embeddings = model.get_output_embeddings().weight.data

        # Compute average embeddings of existing tokens
        input_embeddings_avg = input_embeddings[:-num_new_tokens].mean(
            dim=0, keepdim=True)
        output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(
            dim=0, keepdim=True)

        # Set average embeddings for new special token embeddings
        input_embeddings[-num_new_tokens:] = input_embeddings_avg
        output_embeddings[-num_new_tokens:] = output_embeddings_avg


def find_all_linear_names(args: argparse.Namespace,
                          model: torch.nn.Module) -> List[str]:
    """
    Returns a list of names of all linear layers present in the given model.

    Args:
        args (argparse.Namespace): A namespace containing arguments of the script.
        model (torch.nn.Module): The PyTorch model to extract linear layer names from.

    Returns:
        List[str]: A list of names of all linear layers present in the given model.

    Raises:
        TypeError: If `args` is not an instance of `argparse.Namespace`, or if `model` is not an instance \
            of `torch.nn.Module`.
        ValueError: If `args.bits` is not 4 or 8.

    Example Usage:
        >>> import argparse
        >>> parser = argparse.ArgumentParser()
        >>> parser.add_argument('--bits', type=int)
        >>> args = parser.parse_args(['--bits', '4'])
        >>> model = torch.nn.Sequential(torch.nn.Linear(10, 5), torch.nn.Linear(5, 1))
        >>> find_all_linear_names(args, model)
        ['0', '1']
    """
    # Determine the correct linear layer class based on the value of `args.bits`
    if args.bits == 4:
        cls = bnb.nn.Linear4bit
    elif args.bits == 8:
        cls = bnb.nn.Linear8bitLt
    else:
        torch.nn.Linear

    lora_module_names = set()
    for name, module in model.named_modules():
        # Check if the current module is an instance of the linear layer class
        if isinstance(module, cls):
            # If yes, split the name of the module into its component parts and add the first or last part to the set
            names = name.split('.')
            lora_module_names.add(names[0] if len(names) == 1 else names[-1])

    # Remove 'lm_head' from the set if present (needed for 16-bit)
    if 'lm_head' in lora_module_names:
        lora_module_names.remove('lm_head')

    # Convert the set into a list and return it
    return list(lora_module_names)


def print_trainable_parameters(args: argparse.Namespace,
                               model: torch.nn.Module) -> None:
    """
    Prints the number of trainable parameters in the given model.

    Args:
        args (argparse.Namespace): A namespace containing arguments of the script. Must contain the 'bits' argument.
        model (torch.nn.Module): The PyTorch model to count trainable parameters in.

    Raises:
        TypeError: If `args` is not an instance of `argparse.Namespace`, or if `model` is not an instance \
            of `torch.nn.Module`.

    Example Usage:
        >>> import argparse
        >>> parser = argparse.ArgumentParser()
        >>> parser.add_argument('--bits', type=int)
        >>> args = parser.parse_args(['--bits', '4'])
        >>> model = torch.nn.Sequential(torch.nn.Linear(10, 5), torch.nn.Linear(5, 1))
        >>> print_trainable_parameters(args, model)
        trainable params: 13.0 || all params: 61 || trainable: 21.311475409836067%
    """
    trainable_params = 0
    all_param = 0

    # Iterate through all the named parameters of the model
    for _, param in model.named_parameters():
        all_param += param.numel()
        # Add the number of elements in the parameter tensor to the total count
        if param.requires_grad:  # If the parameter requires gradient computation during backpropagation
            trainable_params += param.numel()
            # Add its number of elements to the trainable parameters count

    # If args.bits is 4, divide the trainable params count by 2 \
    # (since each 4-bit element requires only 2 bits for storage)
    if args.bits == 4:
        trainable_params /= 2

    # Compute and print the percentage of trainable vs all parameters
    trainable_percent = 100 * trainable_params / all_param
    print(f'trainable params: {trainable_params} || '
          f'all params: {all_param} || '
          f'trainable: {trainable_percent}%')


def verify_dtypes(model: torch.nn.Module) -> None:
    """
    检查模型参数的数据类型，并输出各个数据类型在这些张量中所占的比例.

    :param model: 待检查的模型.
    :return: 无返回值.
    """
    # 创建一个空字典dtypes，用于记录模型参数的数据类型及其数量.
    dtypes = {}

    # 遍历模型参数，并统计每种数据类型出现的次数.
    for _, p in model.named_parameters():
        dtype = p.dtype
        if dtype not in dtypes:
            dtypes[dtype] = 0
        dtypes[dtype] += p.numel()

    # 计算总共的参数数量total.
    total = sum(dtypes.values())

    # 输出各个数据类型的数量以及所占比例.
    for k, v in dtypes.items():
        print(f'{k}: {v} ({100 * v / total:.2f}%)')
    return None


def get_last_checkpoint(checkpoint_dir: str) -> Tuple[str, bool]:
    """
    Given a directory containing previous saved checkpoints, returns the path to the last checkpoint
    if available along with a boolean flag indicating whether training has already been completed.

    Args:
        checkpoint_dir (str): Path to the directory containing the saved checkpoints.

    Returns:
        A tuple containing the path to the last checkpoint if available, and a boolean flag indicating
        whether training has already been completed.
    """
    # Check if provided directory exists
    if isdir(checkpoint_dir):

        # Check if 'completed' file exists in the directory - indicates training has completed
        is_completed = exists(join(checkpoint_dir, 'completed'))
        if is_completed:
            return None, True  # Already finished

        # Find the latest checkpoint by checking all subdirectories named 'checkpoint-*'
        max_step = 0
        for filename in os.listdir(checkpoint_dir):
            if isdir(join(checkpoint_dir,
                          filename)) and filename.startswith('checkpoint'):
                max_step = max(max_step,
                               int(filename.replace('checkpoint-', '')))
        if max_step == 0:
            return None, is_completed  # Training started, but no checkpoint found

        # Return path to the latest checkpoint directory
        checkpoint_dir = join(checkpoint_dir, f'checkpoint-{max_step}')
        print(f'Found a previous checkpoint at: {checkpoint_dir}')
        return checkpoint_dir, is_completed

    # The directory does not exist, meaning this is the first time the training is being run
    return None, False


class SavePeftModelCallback(transformers.TrainerCallback):
    """
    A TrainerCallback that saves the PEFT model checkpoint during training.
    """
    def save_model(self, args: Any, state: transformers.TrainingArguments,
                   kwargs: Dict[str, Any]) -> None:
        """
        Saves the PEFT model checkpoint.

        Args:
            args (Any): The command line arguments passed to the script.
            state (transformers.TrainingArguments): The current state of training.
            kwargs (Dict[str, Any]): A dictionary of additional keyword arguments.

        Raises:
            TypeError: If `state` is not an instance of `transformers.TrainingArguments`.
        """
        print('Saving PEFT checkpoint...')

        if state.best_model_checkpoint is not None:
            # If best model checkpoint exists, use its directory as the checkpoint folder
            checkpoint_folder = os.path.join(state.best_model_checkpoint,
                                             'adapter_model')
        else:
            # Otherwise, create a new checkpoint folder using the output directory and current global step
            checkpoint_folder = os.path.join(
                args.output_dir,
                f'{PREFIX_CHECKPOINT_DIR}-{state.global_step}')

        # Create path for the PEFT model
        peft_model_path = os.path.join(checkpoint_folder, 'adapter_model')
        kwargs['model'].save_pretrained(peft_model_path)

        # Create path for the PyTorch model binary file and remove it if it already exists
        pytorch_model_path = os.path.join(checkpoint_folder,
                                          'pytorch_model.bin')
        if os.path.exists(pytorch_model_path):
            os.remove(pytorch_model_path)

    def on_save(
        self, args: Any, state: transformers.TrainingArguments,
        control: transformers.trainer_callback.TrainerControl,
        **kwargs: Dict[str,
                       Any]) -> transformers.trainer_callback.TrainerControl:
        """
        Callback method that calls save_model() and returns `control` argument.

        Args:
            args (Any): The command line arguments passed to the script.
            state (transformers.TrainingArguments): The current state of training.
            control (transformers.trainer_callback.TrainerControl): \
                The current state of the TrainerCallback's control flow.
            kwargs (Dict[str, Any]): A dictionary of additional keyword arguments.

        Returns:
            transformers.trainer_callback.TrainerControl: The current state of the TrainerCallback's control flow.

        Raises:
            TypeError: If `state` is not an instance of `transformers.TrainingArguments`.
        """
        self.save_model(args, state, kwargs)
        return control

    def on_train_end(self, args: Any, state: transformers.TrainingArguments,
                     control: transformers.trainer_callback.TrainerControl,
                     **kwargs: Dict[str, Any]) -> None:
        """
        Callback method that saves the model checkpoint and creates a 'completed' file in the output directory.

        Args:
            args (Any): The command line arguments passed to the script.
            state (transformers.TrainingArguments): The current state of training.
            control (transformers.trainer_callback.TrainerControl): \
                The current state of the TrainerCallback's control flow.
            kwargs (Dict[str, Any]): A dictionary of additional keyword arguments.

        Raises:
            TypeError: If `state` is not an instance of `transformers.TrainingArguments`.
        """

        # Define a helper function to create a 'completed' file in the output directory
        def touch(fname, times=None):
            with open(fname, 'a'):
                os.utime(fname, times)

        # Create the 'completed' file in the output directory
        touch(os.path.join(args.output_dir, 'completed'))

        # Save the model checkpoint
        self.save_model(args, state, kwargs)
