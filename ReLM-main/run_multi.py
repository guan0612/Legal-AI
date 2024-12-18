from __future__ import absolute_import, division, print_function
import argparse
import logging
import os
import random
import math
import copy
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler, TensorDataset
from multiTask.MultiTaskModel import BertForMultiTask
from multiTask.MultiTaskDataset import InputExample, SighanProcessor, EcspellProcessor, TnewsProcessor, AfqmcProcessor
from multiTask.MultiTaskDataset import csc_convert_examples_to_features, seq_convert_examples_to_features
from transformers import SchedulerType
from transformers import AutoTokenizer
from utils.metrics import Metrics
from tqdm.auto import tqdm

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
                    datefmt="%m/%d/%Y %H:%M:%S",
                    level=logging.INFO)
logger = logging.getLogger(__name__)

def mask_tokens(inputs, targets, task_ids, tokenizer, device, mask_mode="noerror", noise_probability=0.2):
    ## mask_mode in ["all","error","noerror"]
    inputs = inputs.clone()
    probability_matrix = torch.full(inputs.shape, noise_probability).to(device)

    inputs_shape = inputs.size()
    csc_task_matrix = torch.ones(inputs_shape).to(device)
    task_ids_expand=task_ids.unsqueeze(dim=-1).expand(inputs_shape)
    probability_matrix.masked_fill_(task_ids_expand!=csc_task_matrix, value=0.0)
    special_tokens_mask = [
        tokenizer.get_special_tokens_mask(val, already_has_special_tokens=True) for val in inputs.tolist()
    ]
    special_tokens_mask = torch.tensor(special_tokens_mask, dtype=torch.bool).to(device)

    probability_matrix.masked_fill_(special_tokens_mask, value=0.0)
    if mask_mode == "noerror":
        probability_matrix.masked_fill_(inputs!=targets, value=0.0)
    elif mask_mode == "error":
        probability_matrix.masked_fill_(inputs==targets, value=0.0)
    else:
        assert mask_mode == "all"
    masked_indices = torch.bernoulli(probability_matrix).bool()
    inputs[masked_indices] = tokenizer.convert_tokens_to_ids(tokenizer.mask_token)

    return inputs

def main():
    parser = argparse.ArgumentParser()

    # Data config.
    parser.add_argument("--data_dir", type=str, default="data/",
                        help="Directory to contain the input data for all tasks.")
    ## mulitple tasks splited by " "
    parser.add_argument("--task_name", type=str, default="SIGHAN tnews afqmc",
                        help="Name of the training task.")
    parser.add_argument("--load_model_path", type=str, default="bert-base-chinese",
                        help="Pre-trained model path to load if needed.")
    parser.add_argument("--cache_dir", type=str, default="../../cache/",
                        help="Directory to store the pre-trained language models downloaded from s3.")
    parser.add_argument("--output_dir", type=str, default="model/",
                        help="Directory to output predictions and checkpoints.")
    parser.add_argument("--load_checkpoint", type=str, default="",
                        help="Trained model weights to load for evaluation.")
    
    # Training config.
    parser.add_argument("--do_train", action="store_true",
                        help="Whether to run training.")
    parser.add_argument("--do_eval", action="store_true",
                        help="Whether to evaluate on the dev set.")
    parser.add_argument("--do_test", action="store_true",
                        help="Whether to evaluate on the test set.")
    ## multiple datasets splited by " "
    parser.add_argument("--train_on", type=str, default="hybrid base base",
                        help="Choose a training set.")
    ## eval and test on only one task
    parser.add_argument("--eval_on", type=str, default="15",
                        help="Choose a dev set.")
    parser.add_argument("--test_on", type=str, default="15",
                        help="Choose a test set.")
    parser.add_argument("--use_slow_tokenizer", action="store_true",
                        help="A slow tokenizer will be used if passed.")
    parser.add_argument("--do_lower_case", action="store_true",
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--max_seq_length", type=int, default=64,
                        help="Maximum total input sequence length after word-piece tokenization.")
    parser.add_argument("--train_batch_size", type=int, default=128,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size", type=int, default=512,
                        help="Total batch size for evaluation.")
    parser.add_argument("--learning_rate", type=float, default=5e-5,
                        help="Initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs", type=float, default=3.0,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--max_train_steps", type=int, default=None,
                        help="Total number of training steps to perform. If provided, overrides training epochs.")
    parser.add_argument("--lr_scheduler_type", type=SchedulerType, default="linear",
                        help="Scheduler type for learning rate warmup.")
    parser.add_argument("--warmup_proportion", type=float, default=0.1,
                        help="Proportion of training to perform learning rate warmup for.")
    parser.add_argument("--weight_decay", type=float, default=0.,
                        help="L2 weight decay for training.")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward pass.")
    parser.add_argument("--no_cuda", action="store_true",
                        help="Whether not to use CUDA when available.")
    parser.add_argument("--fp16", action="store_true",
                        help="Whether to use mixed precision.")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for initialization.")
    parser.add_argument("--save_steps", type=int, default=100,
                        help="How many steps to save the checkpoint once.")
    parser.add_argument("--mft", action="store_true",
                        help="Training with masked-fine-tuning (not published yet).")
    parser.add_argument("--mask_mode", type=str, default="noerror", help="noerror,error or all")
    parser.add_argument("--mask_rate", type=float, default=0.2, help="the percentage we mask the source sentence in mask-ft technique")

    parser.add_argument("--print_para_names", action="store_true", help="only print the parameters' names and do not train" )
    parser.add_argument("--freeze_lm", action="store_true",
                        help="Whether to keep LM parameters frozen.")

    args = parser.parse_args()  

    processors_all = {
        "sighan": SighanProcessor,
        "ecspell": EcspellProcessor,
        "sghspell": SighanProcessor,## the data format in sghspell is the same as sighan
        "tnews": TnewsProcessor,
        "afqmc": AfqmcProcessor,
    }

    task_class={"csc":["sighan","ecspell","sghspell"],
                "seq":["tnews","afqmc"]}

    device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    n_gpu = torch.cuda.device_count()
    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, "Unsupported", args.fp16))

    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    if args.do_train:
        torch.save(args, os.path.join(args.output_dir, "train_args.bin"))

    task_names = args.task_name.lower().split()
    train_on_list = args.train_on.lower().split()
    for task_name in task_names:
        if task_name not in processors_all:
            raise ValueError("Task not found: %s" % task_name)
    # processors is a map containing all the processors we will use
    processors={}
    train_on_dataset={}
    for task_name in task_names:
        processors[task_name]=processors_all[task_name]()
    for train_on,task_name in zip(train_on_list,task_names):
        train_on_dataset[task_name]=train_on

    cache_dir = args.cache_dir
    tokenizer = AutoTokenizer.from_pretrained(args.load_model_path,
                                              do_lower_case=args.do_lower_case,
                                              cache_dir=cache_dir,
                                              use_fast=not args.use_slow_tokenizer,
                                              add_prefix_space=True)
    if args.do_train:
        train_examples = []
        train_features = []
        for task_name,processor in processors.items():
            train_examples_=processor.get_train_examples(os.path.join(args.data_dir, task_name), train_on_dataset[task_name])
            train_examples+=train_examples_
            if task_name in task_class["csc"]:
                train_features += csc_convert_examples_to_features(train_examples_, args.max_seq_length, tokenizer) ## do not apply static mask
            else:
                assert(task_name in task_class["seq"])
                label_list = InputExample.get_label_list(train_examples_)
                print(label_list)
                train_features += seq_convert_examples_to_features(train_examples_, label_list, args.max_seq_length, tokenizer)

        all_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
        ## attention_mask
        all_input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long)
        ## token_type_ids
        all_input_segment = torch.tensor([f.segment_ids for f in train_features], dtype=torch.long)
        all_label_ids = torch.tensor([f.label_ids for f in train_features], dtype=torch.long)##(batch,seq)
        all_task_ids = torch.tensor([f.task_id for f in train_features], dtype=torch.long)

        train_data = TensorDataset(all_input_ids, all_input_mask, all_input_segment, all_label_ids, all_task_ids)
        ## we have to disrupt the order the features from different tasks
        train_sampler = RandomSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)

        num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
        if args.max_train_steps is None:
            args.max_train_steps = int(args.num_train_epochs * num_update_steps_per_epoch)
        else:
            args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

        model = BertForMultiTask.from_pretrained(args.load_model_path,
                                                       return_dict=True,
                                                       cache_dir=cache_dir)
        model.to(device)
        if args.load_checkpoint:
            model.load_state_dict(torch.load(args.load_checkpoint))
        if n_gpu > 1:
            model = torch.nn.DataParallel(model)

        no_decay = ["bias", "LayerNorm.bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": args.weight_decay
            },
            {
                "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0
            }
        ]
        if args.print_para_names:
            classifier_params = ["qmc_","tnews_"]
            for n,p in model.named_parameters():
                if not any(nd in n for nd in classifier_params):##why not nd==n
                    p.requires_grad = False
                print(n,'\n', p.requires_grad)
            return

        #######################################################################
        if args.freeze_lm:##freeze the parameters in the lm except prompt parameters
            classifier_params = ["qmc_","tnews_"]
            for n,p in model.named_parameters():
                if not any(nd in n for nd in classifier_params):##why not nd==n
                    p.requires_grad = False
                    logger.info("Freeze `{}`".format(n))


        optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate)
        '''
        scheduler = get_scheduler(name=args.lr_scheduler_type,
                                  optimizer=optimizer,
                                  num_warmup_steps=args.max_train_steps * args.warmup_proportion,
                                  num_training_steps=args.max_train_steps)
        '''
        

        scaler = None
        if args.fp16:
            from torch.cuda.amp import autocast, GradScaler

            scaler = GradScaler()

        if args.do_eval:
            task_name = task_names[0] ## we choose the first task to evaluate
            processor = processors[task_name]
            eval_examples=processor.get_test_examples(os.path.join(args.data_dir, task_name), args.eval_on)
            if task_name in task_class["csc"]:
                eval_features = csc_convert_examples_to_features(eval_examples, args.max_seq_length, tokenizer) ## no mft in test
            else:
                assert(task_name in task_class["seq"])
                label_list = InputExample.get_label_list(eval_examples)
                eval_features = seq_convert_examples_to_features(eval_examples, label_list, args.max_seq_length, tokenizer)

            all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
            ## attention_mask
            all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
            ## token_type_ids
            all_input_segment = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)
            all_label_ids = torch.tensor([f.label_ids for f in eval_features], dtype=torch.long)
            all_task_ids = torch.tensor([f.task_id for f in eval_features], dtype=torch.long)

            eval_data = TensorDataset(all_input_ids, all_input_mask, all_input_segment, all_label_ids, all_task_ids)
            ## we have to disrupt the order the features from different tasks
            eval_sampler = RandomSampler(eval_data)
            eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)
        
    if args.do_train:
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", args.max_train_steps)

        global_step = 0
        best_result = list()
        wrap = False
        progress_bar = tqdm(range(args.max_train_steps))
        for _ in range(int(args.num_train_epochs)):
            train_loss = 0
            num_train_examples = 0
            if wrap: break
            for step, batch in enumerate(train_dataloader):
                model.train()
                batch = tuple(t.to(device) for t in batch)
                input_ids, attention_mask, token_type_ids, label_ids, task_id = batch
                '''
                print("size of input_ids:{}".format(input_ids.size()))
                print("size of task_id:{}".format(task_id.size()))
                print("size of label_ids:{}".format(label_ids.size()))
                '''
                if args.mft:
                    input_ids = mask_tokens(input_ids, label_ids, task_id, tokenizer, device, mask_mode=args.mask_mode, noise_probability=args.mask_rate)

                if args.fp16:
                    with autocast():
                        outputs = model(input_ids=input_ids, ## (batch,seq)
                                        attention_mask=attention_mask, ## (batch,seq)
                                        token_type_ids=token_type_ids, ## (batch,seq)
                                        task_id = task_id, ## batch
                                        labels=label_ids) ## (batch,seq) or batch
                else:
                    outputs = model(input_ids=input_ids,
                                    attention_mask=attention_mask,
                                    token_type_ids=token_type_ids,
                                    task_id = task_id,
                                    labels=label_ids)
                loss = outputs[0]

                if n_gpu > 1:
                    loss = loss.mean()
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                if args.fp16:
                    scaler.scale(loss).backward()
                else:
                    loss.backward()

                train_loss += loss.item()
                num_train_examples += input_ids.size(0)
                if (step + 1) % args.gradient_accumulation_steps == 0 or step == len(train_dataloader) - 1:
                    if args.fp16:
                        scaler.unscale_(optimizer)
                        scaler.step(optimizer)
                        scaler.update()
                    else:
                        optimizer.step()
                    optimizer.zero_grad()
                    #scheduler.step()
                    global_step += 1
                    progress_bar.update(1)

                if args.do_eval and global_step % args.save_steps == 0:
                    logger.info("***** Running evaluation *****")
                    logger.info("  Num examples = %d", len(eval_examples))
                    logger.info("  Batch size = %d", args.eval_batch_size)

                    def decode(input_ids):
                        return tokenizer.convert_ids_to_tokens(input_ids, skip_special_tokens=True)

                    model.eval()
                    eval_loss = 0
                    eval_steps = 0
                    all_inputs, all_labels, all_predictions = [], [], []
                    for batch in tqdm(eval_dataloader, desc="Evaluation"):
                        batch = tuple(t.to(device) for t in batch)
                        input_ids, attention_mask, token_type_ids, label_ids, task_id = batch
                        with torch.no_grad():
                            outputs = model(input_ids=input_ids,
                                            attention_mask=attention_mask,
                                            token_type_ids=token_type_ids,
                                            task_id = task_id,
                                            labels=label_ids)
                            tmp_eval_loss = outputs[0]
                            logits = outputs[1] ##(batch_size,seq_length,vocab_size) or (batch_size,label_list_size)

                        src_ids = input_ids.cpu().tolist()
                        trg_ids = label_ids.cpu().numpy() ##(batch_size,seq_length)
                        eval_loss += tmp_eval_loss.mean().item()
                        _, prd_ids = torch.max(logits, -1) ##(batch_size,seq_length) or (batch_size)

                        if task_name in task_class["csc"]:
                            prd_ids = prd_ids.masked_fill(attention_mask == 0, 0).tolist()
                            for s, t, p in zip(src_ids, trg_ids, prd_ids):
                                all_inputs += [decode(s)]
                                all_labels += [decode(t)]
                                all_predictions += [decode(p)]
                        else:
                            assert(task_name in task_class["seq"])
                            all_predictions.extend(prd_ids.detach().cpu().numpy().squeeze().tolist())
                            all_labels.extend(trg_ids[:,0].squeeze().tolist())
                        eval_steps += 1
    
                    loss = train_loss / global_step
                    eval_loss = eval_loss / eval_steps
                    if task_name in task_class["csc"]:
                        p, r, f1, fpr, wpr, tp, fp, fn, wp = Metrics.csc_compute(all_inputs, all_labels, all_predictions)
                    else:
                        assert(task_name in task_class["seq"])
                        f1 = Metrics.f1(all_predictions, all_labels)
                        acc = Metrics.acc(all_predictions,all_labels)

                    if task_name in task_class["csc"]:
                        output_tp_file = os.path.join(args.output_dir, "sents.tp")
                        with open(output_tp_file, "w") as writer:
                            for line in tp:
                                writer.write(line + "\n")
                        output_fp_file = os.path.join(args.output_dir, "sents.fp")
                        with open(output_fp_file, "w") as writer:
                            for line in fp:
                                writer.write(line + "\n")
                        output_fn_file = os.path.join(args.output_dir, "sents.fn")
                        with open(output_fn_file, "w") as writer:
                            for line in fn:
                                writer.write(line + "\n")
                        output_wp_file = os.path.join(args.output_dir, "sents.wp")
                        with open(output_wp_file, "w") as writer:
                            for line in wp:
                                writer.write(line + "\n")
                        result = {
                            "global_step": global_step,
                            "loss": loss,
                            "eval_loss": eval_loss,
                            "eval_p": p * 100,
                            "eval_r": r * 100,
                            "eval_f1": f1 * 100,
                            "eval_fpr": fpr * 100,
                        }
                    else:
                        result = {
                            "global_step": global_step,
                            "loss": loss,
                            "eval_loss": eval_loss,
                            "eval_acc": acc*100,
                            "eval_f1": f1 * 100,
                        }
                    model_to_save = model.module if hasattr(model, "module") else model
                    output_model_file = os.path.join(args.output_dir, "step-%s_f1-%.2f.bin" % (str(global_step), result["eval_f1"]))
                    torch.save(model_to_save.state_dict(), output_model_file)
                    best_result.append((result["eval_f1"], output_model_file))
                    ## sort by f1 and remove model whose f1 is the fourth biggest 
                    best_result.sort(key=lambda x: x[0], reverse=True)
                    if len(best_result) > 3:
                        _, model_to_remove = best_result.pop()
                        os.remove(model_to_remove)

                    output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
                    if task_name in task_class['csc']:
                        with open(output_eval_file, "a") as writer:
                            logger.info("***** Eval results *****")
                            writer.write(
                                "Global step = %s | eval precision = %.2f | eval recall = %.2f | eval f1 = %.2f | eval fp rate = %.2f\n"
                                % (str(result["global_step"]),
                                result["eval_p"],
                                result["eval_r"],
                                result["eval_f1"],
                                result["eval_fpr"]))
                            for key in sorted(result.keys()):
                                logger.info("Global step: %s,  %s = %s", str(global_step), key, str(result[key]))
                    else:
                        with open(output_eval_file, "a") as writer:
                            logger.info("***** Eval results *****")
                            writer.write(
                                "Global step = %s |  eval f1 = %.2f |  eval acc = %.2f \n"
                                % (str(result["global_step"]),
                                result["eval_f1"],
                                result["eval_acc"]))
                            for key in sorted(result.keys()):
                                logger.info("Global step: %s,  %s = %s", str(global_step), key, str(result[key]))

                if global_step >= args.max_train_steps:
                    wrap = True
                    break
    if args.do_test:
        task_name = task_names[0] ## we choose the first task to evaluate
        processor = processors[task_name]
        eval_examples=processor.get_test_examples(os.path.join(args.data_dir, task_name), args.test_on)
        if task_name in task_class["csc"]:
            eval_features = csc_convert_examples_to_features(eval_examples, args.max_seq_length, tokenizer) ## no mft in test
        else:
            assert(task_name in task_class["seq"])
            label_list = InputExample.get_label_list(eval_examples)
            eval_features = seq_convert_examples_to_features(eval_examples, label_list, args.max_seq_length, tokenizer)

        all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
        ## attention_mask
        all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
        ## token_type_ids
        all_input_segment = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)
        all_label_ids = torch.tensor([f.label_ids for f in eval_features], dtype=torch.long)
        all_task_ids = torch.tensor([f.task_id for f in eval_features], dtype=torch.long)

        eval_data = TensorDataset(all_input_ids, all_input_mask, all_input_segment, all_label_ids, all_task_ids)
        ## we have to disrupt the order the features from different tasks
        eval_sampler = RandomSampler(eval_data)
        eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

        model = BertForMultiTask.from_pretrained(args.load_model_path,
                                                       return_dict=True,
                                                       cache_dir=cache_dir)
        model.to(device)
        if args.load_checkpoint:
            model.load_state_dict(torch.load(args.load_checkpoint))
        if n_gpu > 1:
            model = torch.nn.DataParallel(model)

        logger.info("***** Running evaluation *****")
        logger.info("  Num examples = %d", len(eval_examples))
        logger.info("  Batch size = %d", args.eval_batch_size)

        def decode(input_ids):
            return tokenizer.convert_ids_to_tokens(input_ids, skip_special_tokens=True)

        model.eval()
        eval_loss = 0
        eval_steps = 0
        all_inputs, all_labels, all_predictions = [], [], []
        for batch in tqdm(eval_dataloader, desc="Evaluation"):
            batch = tuple(t.to(device) for t in batch)
            input_ids, attention_mask, token_type_ids, label_ids, task_id = batch
            with torch.no_grad():
                outputs = model(input_ids=input_ids,
                                attention_mask=attention_mask,
                                token_type_ids=token_type_ids,
                                task_id = task_id,
                                labels=label_ids)
                tmp_eval_loss = outputs[0]
                logits = outputs[1] ##(batch_size,seq_length,vocab_size) or (batch_size,label_list_size)

            src_ids = input_ids.cpu().tolist() ##(batch_size,seq_length)
            trg_ids = label_ids.cpu().numpy() ##(batch_size,seq_length)
            eval_loss += tmp_eval_loss.mean().item()
            _, prd_ids = torch.max(logits, -1) ##(batch_size,seq_length) or (batch_size)
            print("***label_id***")
            print(trg_ids)
            print("***pred_ids***")
            print(prd_ids)

            if task_name in task_class["csc"]:
                prd_ids = prd_ids.masked_fill(attention_mask == 0, 0).tolist()
                for s, t, p in zip(src_ids, trg_ids, prd_ids):
                    all_inputs += [decode(s)]
                    all_labels += [decode(t)]
                    all_predictions += [decode(p)]
            else:
                assert(task_name in task_class["seq"])
                all_predictions.extend(prd_ids.detach().cpu().numpy().squeeze().tolist())
                all_labels.extend(trg_ids[:,0].squeeze().tolist())
            eval_steps += 1

        eval_loss = eval_loss / eval_steps
        if task_name in task_class["csc"]:
            p, r, f1, fpr, wpr, tp, fp, fn, wp = Metrics.csc_compute(all_inputs, all_labels, all_predictions)
        else:
            assert(task_name in task_class["seq"])
            f1 = Metrics.f1(all_predictions, all_labels)
            acc = Metrics.acc(all_predictions,all_labels)

        if task_name in task_class["csc"]:
            output_tp_file = os.path.join(args.output_dir, "sents.tp")
            with open(output_tp_file, "w") as writer:
                for line in tp:
                    writer.write(line + "\n")
            output_fp_file = os.path.join(args.output_dir, "sents.fp")
            with open(output_fp_file, "w") as writer:
                for line in fp:
                    writer.write(line + "\n")
            output_fn_file = os.path.join(args.output_dir, "sents.fn")
            with open(output_fn_file, "w") as writer:
                for line in fn:
                    writer.write(line + "\n")
            output_wp_file = os.path.join(args.output_dir, "sents.wp")
            with open(output_wp_file, "w") as writer:
                for line in wp:
                    writer.write(line + "\n")
            result = {
                "eval_loss": eval_loss,
                "eval_p": p * 100,
                "eval_r": r * 100,
                "eval_f1": f1 * 100,
                "eval_fpr": fpr * 100,
            }
        else:
            result = {
                "eval_loss": eval_loss,
                "eval_acc": acc*100,
                "eval_f1": f1 * 100,
            }

        output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
        if task_name in task_class['csc']:
            with open(output_eval_file, "a") as writer:
                logger.info("***** Eval results *****")
                writer.write(
                    "Global step = %s | eval precision = %.2f | eval recall = %.2f | eval f1 = %.2f | eval fp rate = %.2f\n"
                    % (str(-1),
                    result["eval_p"],
                    result["eval_r"],
                    result["eval_f1"],
                    result["eval_fpr"]))
                for key in sorted(result.keys()):
                    logger.info("Global step: %s,  %s = %s", str(-1), key, str(result[key]))
        else:
            with open(output_eval_file, "a") as writer:
                logger.info("***** Eval results *****")
                writer.write(
                    "Global step = %s |  eval f1 = %.2f |  eval acc = %.2f \n"
                    % (str(-1),
                    result["eval_f1"],
                    result["eval_acc"]))
                for key in sorted(result.keys()):
                    logger.info("Global step: %s,  %s = %s", str(-1), key, str(result[key]))

if __name__ == "__main__":
    main()
