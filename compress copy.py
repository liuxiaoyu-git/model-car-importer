import torch
from datasets import load_dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from llmcompressor.modifiers.quantization import GPTQModifier
from llmcompressor import oneshot
import gc
import os
import json
import shutil

def get_directory_size(path):
    """Calculate total size of all files in a directory."""
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                total_size += os.path.getsize(fp)
    return total_size

def format_size(size_bytes):
    """Convert size in bytes to human readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"

# Print GPU information
print(f"Number of GPUs available: {torch.cuda.device_count()}")
for i in range(torch.cuda.device_count()):
    print(f"GPU {i}: {torch.cuda.get_device_name(i)}")
    print(f"GPU {i} memory: {torch.cuda.get_device_properties(i).total_memory / 1024**3:.2f} GB")

# Enable memory efficient attention
torch.backends.cuda.enable_mem_efficient_sdp(True)
torch.backends.cuda.enable_flash_sdp(True)

# Clear any existing CUDA cache
torch.cuda.empty_cache()
gc.collect()

# Set model directories
base_dir = '/workspace/shared-workspace'
model_dir = os.path.join(base_dir, 'model')
compressed_dir = os.path.join(base_dir, 'compressed_model')
original_dir = os.path.join(base_dir, 'model_original')

# Create compressed model directory if it doesn't exist
os.makedirs(compressed_dir, exist_ok=True)

print(f"Current working directory: {os.getcwd()}")
print(f"Model directory contents: {os.listdir(model_dir)}")

# Calculate original model size
original_size = get_directory_size(model_dir)
print(f"\nOriginal model size: {format_size(original_size)}")

# Check model index file
index_file = os.path.join(model_dir, 'model.safetensors.index.json')
if os.path.exists(index_file):
    with open(index_file, 'r') as f:
        index_data = json.load(f)
        print(f"Model index data: {index_data}")

# Calculate memory per GPU - more conservative allocation
num_gpus = torch.cuda.device_count()
memory_per_gpu = 16  # Reduced from 20GB to 16GB per GPU
total_memory = {i: f"{memory_per_gpu}GiB" for i in range(num_gpus)}

# Load model and tokenizer with memory optimizations
model = AutoModelForCausalLM.from_pretrained(
    model_dir,
    device_map='auto',  # This will automatically distribute the model across available GPUs
    torch_dtype=torch.float16,
    low_cpu_mem_usage=True,
    offload_folder="offload",
    max_memory=total_memory,  # Distribute memory across all GPUs
)
tokenizer = AutoTokenizer.from_pretrained(model_dir)

# Select calibration dataset
DATASET_ID = 'HuggingFaceH4/ultrachat_200k'
DATASET_SPLIT = 'train_sft'
NUM_CALIBRATION_SAMPLES = 16 * num_gpus  # Reduced from 32 to 16 per GPU
MAX_SEQUENCE_LENGTH = 64  # Further reduced for memory efficiency

# Load dataset and preprocess
ds = load_dataset(DATASET_ID, split=f'{DATASET_SPLIT}[:{NUM_CALIBRATION_SAMPLES}]')
ds = ds.shuffle(seed=42)

def preprocess(example):
    return {
        'text': tokenizer.apply_chat_template(
            example['messages'],
            tokenize=False,
        )
    }

ds = ds.map(preprocess, num_proc=1)  # Single process to reduce memory usage

# Tokenize inputs
def tokenize(sample):
    return tokenizer(
        sample['text'],
        padding=False,
        max_length=MAX_SEQUENCE_LENGTH,
        truncation=True,
        add_special_tokens=False,
    )

ds = ds.map(tokenize, remove_columns=ds.column_names, num_proc=1)  # Single process to reduce memory usage

# Configure quantization with memory-efficient settings
recipe = GPTQModifier(
    targets='Linear',
    scheme='W4A16',
    ignore=['lm_head'],
    group_size=16,  # Further reduced group size for better memory efficiency
    act_order=False,  # Disable activation reordering for speed
)

# Clear memory before compression
torch.cuda.empty_cache()
gc.collect()

# Apply compression with memory optimizations
oneshot(
    model=model,
    dataset=ds,
    recipe=recipe,
    max_seq_length=MAX_SEQUENCE_LENGTH,
    num_calibration_samples=NUM_CALIBRATION_SAMPLES,
    output_dir=compressed_dir,  # Use separate directory for compressed model
)

# Clear CUDA cache before saving
torch.cuda.empty_cache()
gc.collect()

# Save compressed model with memory optimizations
print("Starting to save compressed model...")

# Move model to CPU before saving to reduce GPU memory usage
model = model.cpu()
torch.cuda.empty_cache()
gc.collect()

# Save model with memory-efficient settings
model.save_pretrained(
    compressed_dir,
    save_compressed=True,
    max_shard_size="2GB",  # Split into smaller shards
    safe_serialization=True,  # Use safetensors format
)

# Clear memory after saving
del model
torch.cuda.empty_cache()
gc.collect()

# Save tokenizer
tokenizer.save_pretrained(compressed_dir)

# Calculate compressed model size
compressed_size = get_directory_size(compressed_dir)
print(f"\nCompressed model size: {format_size(compressed_size)}")
print(f"Size reduction: {format_size(original_size - compressed_size)}")
print(f"Compression ratio: {original_size/compressed_size:.2f}x")

print("\nRenaming directories...")
# First rename the original model directory
if os.path.exists(original_dir):
    shutil.rmtree(original_dir)
os.rename(model_dir, original_dir)

# Then rename the compressed directory to model
os.rename(compressed_dir, model_dir)

print("Compression and directory reorganization complete!") 