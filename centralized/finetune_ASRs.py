# =============================================================
# 更新2023/04/10
# 1. csv2dataset函數裡面使用csv_path和root_path
# 2. 在讀音檔的時候增加一個選項：scipy.io，讀起來會快很多但是不知道會不會影響到原來的效果

# 大約10138MiB
# =============================================================

import torch
import numpy as np
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
import os.path
import pandas as pd
from datasets import Dataset, load_from_disk
import librosa
from datasets import load_dataset, load_metric
from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor, Trainer, TrainingArguments
from transformers import Data2VecAudioConfig, HubertConfig, SEWDConfig, UniSpeechSatConfig
from transformers import Data2VecAudioForCTC, HubertForCTC, SEWDForCTC, UniSpeechSatForCTC
from jiwer import wer
import scipy.io
from utils import csv2dataset, WriteResult

# set up trainer
@dataclass
class DataCollatorCTCWithPadding:
    """
    Data collator that will dynamically pad the inputs received.
    Args:
        processor (:class:`~transformers.Wav2Vec2Processor`)
            The processor used for proccessing the data.
        padding (:obj:`bool`, :obj:`str` or :class:`~transformers.tokenization_utils_base.PaddingStrategy`, `optional`, defaults to :obj:`True`):
            Select a strategy to pad the returned sequences (according to the model's padding side and padding index)
            among:
            * :obj:`True` or :obj:`'longest'`: Pad to the longest sequence in the batch (or no padding if only a single
              sequence if provided).
            * :obj:`'max_length'`: Pad to a maximum length specified with the argument :obj:`max_length` or to the
              maximum acceptable input length for the model if that argument is not provided.
            * :obj:`False` or :obj:`'do_not_pad'` (default): No padding (i.e., can output a batch with sequences of
              different lengths).
        max_length (:obj:`int`, `optional`):
            Maximum length of the ``input_values`` of the returned list and optionally padding length (see above).
        max_length_labels (:obj:`int`, `optional`):
            Maximum length of the ``labels`` returned list and optionally padding length (see above).
        pad_to_multiple_of (:obj:`int`, `optional`):
            If set will pad the sequence to a multiple of the provided value.
            This is especially useful to enable the use of Tensor Cores on NVIDIA hardware with compute capability >=
            7.5 (Volta).
    """

    processor: Wav2Vec2Processor
    padding: Union[bool, str] = True
    max_length: Optional[int] = None
    max_length_labels: Optional[int] = None
    pad_to_multiple_of: Optional[int] = None
    pad_to_multiple_of_labels: Optional[int] = None

    def __call__(self, features: List[Dict[str, Union[List[int], torch.Tensor]]]) -> Dict[str, torch.Tensor]:
        # split inputs and labels since they have to be of different lenghts and need
        # different padding methods
        input_features = [{"input_values": feature["input_values"]} for feature in features]
        label_features = [{"input_ids": feature["labels"]} for feature in features]

        batch = self.processor.pad(
            input_features,
            padding=self.padding,
            max_length=self.max_length,
            pad_to_multiple_of=self.pad_to_multiple_of,
            return_tensors="pt",
        )
        with self.processor.as_target_processor():
            labels_batch = self.processor.pad(
                label_features,
                padding=self.padding,
                max_length=self.max_length_labels,
                pad_to_multiple_of=self.pad_to_multiple_of_labels,
                return_tensors="pt",
            )

        # replace padding with -100 to ignore loss correctly
        labels = labels_batch["input_ids"].masked_fill(labels_batch.attention_mask.ne(1), -100)

        batch["labels"] = labels

        return batch

def prepare_dataset(batch):
    audio = batch["array"]

    # batched output is "un-batched" to ensure mapping is correct
    batch["input_values"] = processor(audio, sampling_rate=16000).input_values[0]
    
    with processor.as_target_processor():
        batch["labels"] = processor(batch["text"]).input_ids
    return batch

def compute_metrics(pred):
    wer_metric = load_metric("wer")
    pred_logits = pred.predictions
    pred_ids = np.argmax(pred_logits, axis=-1)

    pred.label_ids[pred.label_ids == -100] = processor.tokenizer.pad_token_id

    pred_str = processor.batch_decode(pred_ids)
    # we do not want to group tokens when computing the metrics
    label_str = processor.batch_decode(pred.label_ids, group_tokens=False)

    wer = wer_metric.compute(predictions=pred_str, references=label_str)

    return {"wer": wer}

def map_to_result(batch):
    with torch.no_grad():
        input_values = torch.tensor(batch["input_values"]).unsqueeze(0)
        logits = new_model(input_values).logits

    pred_ids = torch.argmax(logits, dim=-1)
    batch["pred_str"] = new_processor.batch_decode(pred_ids)[0]
    batch["text"] = new_processor.decode(batch["labels"], group_tokens=False)
  
    return batch

import argparse

parser = argparse.ArgumentParser()
#parser.add_argument('-model', '--model_path', type=str, default="./saves/wav2vec2-base-960h_GRL_0.5", help="Where the model is saved")
parser.add_argument('-opt', '--optimizer', type=str, default="adamw_hf", help="The optimizer to use: adamw_hf, adamw_torch, adamw_apex_fused, or adafactor")
parser.add_argument('-MGN', '--max_grad_norm', type=float, default=1.0, help="Maximum gradient norm (for gradient clipping)")
parser.add_argument('-model_type', '--model_type', type=str, default="data2vec", help="Type of the model")
parser.add_argument('-sr', '--sampl_rate', type=float, default=16000, help="librosa read smping rate")
parser.add_argument('-lr', '--learning_rate', type=float, default=1e-5, help="Learning rate")
parser.add_argument('-RD', '--root_dir', default='/mnt/Internal/FedASR/Data/ADReSS-IS2020-data', help="Learning rate")
parser.add_argument('--AudioLoadFunc', default='librosa', help="用scipy function好像可以比較快")
args = parser.parse_args()



#model_out_dir = args.model_path # where to save model
model_type = args.model_type                # what type of the model
lr = args.learning_rate                     # learning rate
optim = args.optimizer                      # opt
max_grad_norm = args.max_grad_norm          # max_grad_norm


# load in train-dev-test
train_data = csv2dataset(audio_path = '{}/clips/'.format(args.root_dir),
                         csv_path = "{}/mid_csv/train.csv".format(args.root_dir)) #!!! librosa在load的時候非常慢，大約7分47秒讀完1869個file
dev_data = csv2dataset(audio_path = '{}/clips/'.format(args.root_dir),
                       csv_path = "{}/mid_csv/dev.csv".format(args.root_dir))
test_data = csv2dataset(audio_path = '{}/clips/'.format(args.root_dir),
                        csv_path = "{}/mid_csv/test.csv".format(args.root_dir))

if model_type == "wav2vec":
    name = "facebook/wav2vec2-base-960h" # + model_dir.split("/")[-3]
    model = Wav2Vec2ForCTC.from_pretrained(name)
    print("Current model: ", name)
    processor = Wav2Vec2Processor.from_pretrained(name)
elif model_type == "data2vec":
    name = "facebook/data2vec-audio-large-960h" # + model_in_dir.split("/")[-3]
    print("Current model: ", name)
    mask_time_prob = 0                                                                     # change config
    config = Data2VecAudioConfig.from_pretrained(name, mask_time_prob=mask_time_prob)
    model = Data2VecAudioForCTC.from_pretrained(name, config=config)
    processor = Wav2Vec2Processor.from_pretrained(name)
elif model_type == "hubert":
    name = "facebook/hubert-xlarge-ls960-ft" # + model_in_dir.split("/")[-3]
    print("Current model: ", name)
    mask_time_prob = 0                                                                     # change config
    config = HubertConfig.from_pretrained(name, mask_time_prob=mask_time_prob)
    model = HubertForCTC.from_pretrained(name, config=config)
    processor = Wav2Vec2Processor.from_pretrained(name)
elif model_type == "sewd":
    name = "asapp/sew-d-mid-400k-ft-ls100h" #+ model_in_dir.split("/")[-3]
    print("Current model: ", name)
    mask_time_prob = 0                                                                     # change config
    config = SEWDConfig.from_pretrained(name, mask_time_prob=mask_time_prob)
    model = SEWDForCTC.from_pretrained(name, config=config)
    processor = Wav2Vec2Processor.from_pretrained(name)
elif model_type == "unispeech":
    name = "microsoft/unispeech-sat-base-100h-libri-ft" # + model_in_dir.split("/")[-3]
    print("Current model: ", name)
    mask_time_prob = 0                                                                     # change config
    config = UniSpeechSatConfig.from_pretrained(name, mask_time_prob=mask_time_prob)
    model = UniSpeechSatForCTC.from_pretrained(name, config=config)
    processor = Wav2Vec2Processor.from_pretrained(name)
else:
    print("WRONG TYPE!!!!!!!!!!!!!!!!")


# use processor to get labels
# 在這段程式碼中，map() 是一個運用在 test_data 上的函式，它的目的是將 test_data 中的每個元素都應用到 map_to_result 函式上，並生成一個新的結果序列。
# datasets object 通常使用.map()函數更改裡面預設的變數
train_data = train_data.map(prepare_dataset, num_proc=4)
dev_data = dev_data.map(prepare_dataset, num_proc=4)
test_data = test_data.map(prepare_dataset, num_proc=4)

data_collator = DataCollatorCTCWithPadding(processor=processor, padding=True)

model.freeze_feature_encoder()

training_args = TrainingArguments(
    output_dir="./saves/" + name.split("/")[-1] + "_finetuned",
    group_by_length=True,
    per_device_train_batch_size=1,
    per_device_eval_batch_size=1,
    evaluation_strategy="steps",
    num_train_epochs=30,
    fp16=True,
    gradient_checkpointing=True, 
    save_steps=500,
    eval_steps=500,
    logging_steps=500,
    learning_rate=lr,
    weight_decay=0.005,
    warmup_steps=1000,
    save_total_limit=2,
    optim=optim,
    max_grad_norm=max_grad_norm,
)

trainer = Trainer(
    model=model,
    data_collator=data_collator,
    args=training_args,
    compute_metrics=compute_metrics,
    train_dataset=train_data,
    eval_dataset=dev_data,
    tokenizer=processor.feature_extractor,
)
trainer.train()
Save_path="./saves/" + name.split("/")[-1] + "_finetuned/final"
trainer.save_model(Save_path)

# load in trained model
if model_type == "wav2vec":
    new_model = Wav2Vec2ForCTC.from_pretrained(Save_path)
    new_processor = Wav2Vec2Processor.from_pretrained(name)
elif model_type == "data2vec":
    new_model = Data2VecAudioForCTC.from_pretrained(Save_path)
    new_processor = Wav2Vec2Processor.from_pretrained(name)
elif model_type == "hubert":
    new_model = HubertForCTC.from_pretrained(Save_path)
    new_processor = Wav2Vec2Processor.from_pretrained(name)
elif model_type == "sewd":
    new_model = SEWDForCTC.from_pretrained(Save_path)
    new_processor = Wav2Vec2Processor.from_pretrained(name)
elif model_type == "unispeech":
    new_model = UniSpeechSatForCTC.from_pretrained(Save_path)
    new_processor = Wav2Vec2Processor.from_pretrained(name)
else:
    print("WRONG TYPE!!!!!!!!!!!!!!!!")

result = test_data.map(map_to_result)
print("WER of ", name, " : ", wer(result["text"], result["pred_str"]))
WriteResult(result,Save_path)
print("DONE!")
