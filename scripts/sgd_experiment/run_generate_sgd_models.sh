#!/bin/bash

# Default values
NUM_WORKERS=64
INTERNAL_RESULT_PATH="/home/snek/martind/fdp/revision/rrr"
MODEL_NAME="convnet_balanced"
NUM_TRAIN_SAMPLES=1000
NUM_TEST_SAMPLES=1000
DATABASE_SIZE=1000
EPOCHS=25
INTERMEDIATE_EPOCH_LIST="[1, 5, 10, 15, 25]"
# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --num_train_samples)
            NUM_TRAIN_SAMPLES="$2"
            shift 2
            ;;
        --num_test_samples)
            NUM_TEST_SAMPLES="$2"
            shift 2
            ;;
        --database_size)
            DATABASE_SIZE="$2"
            shift 2
            ;;
        --epochs)
            EPOCHS="$2"
            shift 2
            ;;
        --num_workers)
            NUM_WORKERS="$2"
            shift 2
            ;;
        --internal_result_path)
            INTERNAL_RESULT_PATH="$2"
            shift 2
            ;;
        --model_name)
            MODEL_NAME="$2"
            shift 2
            ;;
        --intermediate_epoch_list)
            INTERMEDIATE_EPOCH_LIST="$2"
            shift 2
            ;;
        *)
            echo "Unknown parameter: $1"
            exit 1
            ;;
    esac
done

# Print configuration
echo "Running generate_sgd_models.py with:"
echo "Number of training samples: $NUM_TRAIN_SAMPLES"
echo "Number of test samples: $NUM_TEST_SAMPLES"
echo "Database size: $DATABASE_SIZE"
echo "Number of epochs: $EPOCHS"
echo "Number of workers: $NUM_WORKERS"
echo "Internal result path: $INTERNAL_RESULT_PATH"
echo "Model name: $MODEL_NAME"
echo "Intermediate epochs: $INTERMEDIATE_EPOCH_LIST"
# Run the script

python3 scripts/sgd_experiment/generate_sgd_models.py \
        --num_train_samples $NUM_TRAIN_SAMPLES \
        --num_test_samples $NUM_TEST_SAMPLES \
        --database_size $DATABASE_SIZE \
        --epochs $EPOCHS \
        --num_workers $NUM_WORKERS \
        --internal_result_path $INTERNAL_RESULT_PATH \
        --model_name $MODEL_NAME \
        --intermediate_epoch_list "$INTERMEDIATE_EPOCH_LIST"
