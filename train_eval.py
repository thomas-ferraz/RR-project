# -*- coding: utf-8 -*-
"""whisper_eval.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1x5YTcG93Xjh3F3kzTtHxBs1bEsY2YymE
"""

import argparse
from dataclasses import dataclass
from typing import Any, Dict, List, Union
from functools import partial
import json
import os
import gdown
import numpy as np
import pandas as pd

import torch
from datasets import load_dataset, DatasetDict, Audio
from transformers import (WhisperFeatureExtractor, WhisperTokenizer, 
                          WhisperProcessor, WhisperForConditionalGeneration)
from transformers import Seq2SeqTrainer, Seq2SeqTrainingArguments
from transformers import EarlyStoppingCallback
import evaluate

import audio_degrader as ad

from data_utils import (prepare_audio, apply_degradation,
                                prepare_dataset,
                                DataCollator,
                                DataCollatorwithDegradation,
                                evaluate_robustness)

import logging

def arg_parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Training and evaluation",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--size", type=str, help="Model size", default="tiny"
    )
    parser.add_argument(
        "--finetuned", type=int, help="", default=0
    )    
    parser.add_argument(
        "--dataset", type=str, help="Dataset name", default="google/fleurs"
    )
    parser.add_argument(
        "--lang", type=str, help="Language code", default="gl"
    )
    parser.add_argument(
        "--task", type=str, help="Task for fine-tuning", default="transcribe"
    ) #maybe do a boolean

    parser.add_argument(
        "--output_dir", type=str, help="", default="./model"
    )
    parser.add_argument(
        "--test_cpu_mode", type=bool, help="", default=False
    )
    parser.add_argument(
        "--per_device_train_batch_size", type=int, help="", default=32
    )
    parser.add_argument(
        "--gradient_accumulation_steps", type=int, help="Increase by 2x for every 2x decrease in batch size", default=1
    )
    parser.add_argument(
        "--learning_rate", type=float, help="", default=1e-5
    )
    parser.add_argument(
        "--warmup_steps", type=int, help="", default=500
    )
    parser.add_argument(
        "--max_steps", type=int, help="", default=4000
    )
    parser.add_argument(
        "--gradient_checkpointing", type=int, help="", default=1
    )
    parser.add_argument(
        "--fp16", type=int, help="", default=1
    )
    parser.add_argument(
        "--per_device_eval_batch_size", type=int, help="", default=8
    )
    parser.add_argument(
        "--eval_steps", type=int, help="", default=200
    )
    parser.add_argument(
        "--logging_steps", type=int, help="", default=20
    )
    parser.add_argument(
        "--dataset_streaming", type=int, help="", default=0
    )
    parser.add_argument(
        "--train", type=int, help="0->eval, 1->train+eval", default=1
    )
    parser.add_argument(
        "--eval_robustness", type=int, help="", default=0,
    )
    parser.add_argument(
        "--degradations_path", type=str, help="path to degradation json", 
        default=None,
    )
    parser.add_argument(
        "--debug", type=int, help="", default=0
    )
    parser.add_argument(
      "--predict", type=str, help="", default=0,
    )
    parser.add_argument(
      "--normalize", type=int, help="normalized wer", default=0,
    )
    parser.add_argument
    # TO DO - Help comments in the arguments
    args = parser.parse_args()
    return args

lang_to_whisper = {"gl":"Galician", 
                   "fr":"French", 
                   "fa":"Persian", 
                   "libri_en": "English"}
lang_to_id = {"gl":"gl_es", 
              "fr":"fr_fr", 
              "fa":"fa_ir", 
              "libri_en":"clean"}

def load_finetuned(size="tiny", language="French"):
  """Download finetuned weights from drive"""
  if not os.path.isdir("./finetuned"):
    os.mkdir("./finetuned")
  # Load dictionary of ids
  with open("./finetuned_models.json") as file:
    dict_finetuned = json.load(file)  
  # Download 
  for f, file_id in dict_finetuned[size][language].items():
    gdown.download(f"https://drive.google.com/uc?id={file_id}&confirm=t",
                 output=f"./finetuned/{f}",
                 use_cookies=False)


def compute_metrics(pred, tokenizer, metric_wer, normalize = False):
    pred_ids = pred.predictions
    label_ids = pred.label_ids

    # replace -100 with the pad_token_id
    label_ids[label_ids == -100] = tokenizer.pad_token_id
    
    # we do not want to group tokens when computing the metrics
    pred_str = tokenizer.batch_decode(pred_ids, skip_special_tokens=True)
    label_str = tokenizer.batch_decode(label_ids, skip_special_tokens=True)

    if normalize:
      pred_str = [tokenizer._normalize(pred_str[0])]
      label_str = [tokenizer._normalize(label_str[0])]
    else:
      pred_str = [pred_str[0].lower()]
      label_str = [label_str[0].lower()]      

    wer = 100 * metric_wer.compute(predictions=pred_str, references=label_str)
    
    return {"wer": wer}


def main():

    args = arg_parse()
    ## Verif params
    assert args.size in ["tiny", "base"], "Supported model sizes are tiny and base." 

    ## Load datasets
    dataset = DatasetDict()
    train = bool(args.train)
    if train:
      # Load train and val only for training
      dataset["train"] = load_dataset(args.dataset, lang_to_id[args.lang], 
                                      split="train", 
                                      streaming=bool(args.dataset_streaming))
      dataset["val"] = load_dataset(args.dataset, lang_to_id[args.lang], 
                                      split="validation", 
                                      streaming=bool(args.dataset_streaming))
    dataset["test"] = load_dataset(args.dataset, lang_to_id[args.lang], 
                                   split="test", 
                                   streaming=bool(args.dataset_streaming))
    print(dataset)
    ## Load degradations
    eval_robustness = bool(args.eval_robustness)
    if (args.degradations_path is not None) and (not eval_robustness):
      with open(args.degradations_path) as json_file:
        list_degradations = json.load(json_file)
    else:
      list_degradations = None
    
    ## Debug settings
    if bool(args.debug):
      for s, d in dataset.items():
        dataset[s] = dataset[s].select(list(range(0, 5)))

    if args.test_cpu_mode:
        dataset["train"] = dataset["train"].select(list(range(0, 10)))
        dataset["val"] = dataset["val"].select(list(range(0, 10)))
        dataset["test"] = dataset["test"].select(list(range(0, 10)))
        print(dataset)
        args.max_steps=10
        args.fp16=0
        args.warmup_steps=1
        args.eval_steps=2
        args.logging_steps=1
    
    ## Load pretrained/finetuned
    if bool(args.finetuned):
      # load from drive
      try:
        load_finetuned(args.size, lang_to_whisper[args.lang])
      except Exception as e:
        print("\nFailed to download finetuned weights from Drive." 
              "Please refresh or try later.\n")
        print(e)
        exit()
      model_name_or_path = "./finetuned"
      with open("./finetuned/config.json") as file:
        config = json.load(file)  
      architecture = config['_name_or_path']
      print(f"\nLoaded model: finetuned/{args.size}/{args.lang}\n")
    else:
      model_name_or_path = "openai/whisper-"+args.size
      architecture = model_name_or_path
      print(f"\nLoaded model: pretrained/{args.size}/{args.lang}\n")

    ## Instanciate Whisper Pipeline
    feature_extractor = WhisperFeatureExtractor.from_pretrained(model_name_or_path)
    tokenizer = WhisperTokenizer.from_pretrained(architecture, 
                                                language=lang_to_whisper[args.lang], 
                                                task=args.task)
    processor = WhisperProcessor(feature_extractor, tokenizer)
    model = WhisperForConditionalGeneration.from_pretrained(model_name_or_path) 
    model.config.forced_decoder_ids = None
    model.config.suppress_tokens = []

    ## Metric
    metric = evaluate.load("wer")
    compute_metrics_func = partial(compute_metrics, tokenizer=tokenizer, 
                                                    metric_wer=metric,
                                                    normalize=bool(args.normalize))

    ## Original preprocessing pipeline (No data augmentation) 
    if (list_degradations is None) and (not eval_robustness):
      dataset = dataset.cast_column("audio", Audio(sampling_rate=16000))
      prepare_dataset_func = partial(prepare_dataset, 
                                     tokenizer=tokenizer, 
                                     feature_extractor=feature_extractor,
                                     dataset=args.dataset)
      dataset = dataset.map(prepare_dataset_func, 
                            remove_columns=dataset.column_names["train"], 
                            num_proc=2)
      data_collator = DataCollator(processor=processor)
    
    ## Preprocessing pipeline with data augmentation
    else:
      data_collator = DataCollatorwithDegradation(processor, tokenizer,
                                                  feature_extractor,
                                                  args.dataset,
                                                  list_degradations)
    if train:
      # Perform training and evaluation
      training_args = Seq2SeqTrainingArguments(
          output_dir= args.output_dir,# "./whisper-small-gl"
          per_device_train_batch_size=args.per_device_train_batch_size,
          gradient_accumulation_steps=args.gradient_accumulation_steps, 
          learning_rate=args.learning_rate,
          warmup_steps=args.warmup_steps,#500,
          max_steps=args.max_steps, #1000, #4000,
          gradient_checkpointing=bool(args.gradient_checkpointing),
          fp16=bool(args.fp16),
          evaluation_strategy="steps",
          per_device_eval_batch_size=args.per_device_eval_batch_size,
          predict_with_generate=True,
          generation_max_length=225,
          save_steps=args.eval_steps, #100, #1000
          eval_steps=args.eval_steps,#100, #1000
          logging_steps=args.logging_steps,#25,
          #report_to=["tensorboard"],
          load_best_model_at_end=True,
          metric_for_best_model="wer",
          greater_is_better=False,
          push_to_hub=False,
          save_total_limit=2,
          remove_unused_columns = False,
      )

      early_stop = EarlyStoppingCallback(early_stopping_patience=args.patience,
                                       early_stopping_threshold=args.early_stopping_threshold)

      trainer = Seq2SeqTrainer(
          args=training_args,
          model=model,
          train_dataset=dataset["train"],
          eval_dataset=dataset["val"],
          data_collator=data_collator,
          compute_metrics=compute_metrics_func,
          callbacks=[early_stop],
          tokenizer=processor.feature_extractor,
      )

      trainer.train()

      print("History")
      log_steps = trainer.state.log_history.copy()
      print("End History")
      # No data augmentations for evaluation
      data_collator.list_degradations = None
      test_metrics = trainer.evaluate(dataset["test"])
      test_metrics = {k.replace("eval","test"):v for k,v in test_metrics.items()}
      print(test_metrics)
      log_steps.append(test_metrics)

      with open(os.path.join(args.output_dir,'training_logg.json'), 'w') as file:
          file.write(json.dumps(log_steps, indent=4))
          print(f"Logging history saved at: {os.path.join(args.output_dir,'training_logg.json')}")

      trainer.save_model()

    else:
      ## Only perform evaluation
      # Force task and language
      forced_decoder_ids = processor.get_decoder_prompt_ids(
                                            language=lang_to_whisper[args.lang], 
                                            task="transcribe")
      model.generation_config.forced_decoder_ids = forced_decoder_ids
      
      training_args = Seq2SeqTrainingArguments(
          output_dir= args.output_dir,
          fp16=True,
          per_device_eval_batch_size=args.per_device_eval_batch_size,
          predict_with_generate=True,
          generation_max_length=225, 
          metric_for_best_model="wer",
          remove_unused_columns = False,
          push_to_hub=False,
      )
      trainer = Seq2SeqTrainer(
          args=training_args,
          model=model,
          eval_dataset=dataset["test"],
          data_collator=data_collator,
          compute_metrics=compute_metrics_func,
          tokenizer=processor.feature_extractor,
      )
      # TODO: save results in output directory
      if eval_robustness:
        evaluate_robustness(trainer=trainer, 
                            data_collator=data_collator, 
                            degradation_path=args.degradations_path)

      else:
        prediction_output = trainer.predict(dataset["test"],
                                      metric_key_prefix="test")
        generated_ids = prediction_output.predictions
        transcriptions = processor.batch_decode(generated_ids, 
                                                      skip_special_tokens=True)
        labels = processor.batch_decode(prediction_output.label_ids, 
                                                    skip_special_tokens=True)
        df_predictions = pd.DataFrame()
        df_predictions["labels"] = labels
        df_predictions["transcribed"] = transcriptions

        if bool(args.normalize):
          df_predictions["labels_norm"] = [tokenizer._normalize(text) 
                                            for text in labels]
          df_predictions["transcribed_norm"] = [tokenizer._normalize(text) 
                                            for text in transcriptions]
        
        df_predictions.to_csv("predictions.csv")

        metrics = prediction_output.metrics
        print(pd.Series(metrics))


if __name__ == '__main__':
    main()