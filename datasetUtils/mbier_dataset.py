import os
import json
from tqdm import tqdm
import random
from PIL import Image
from torch.utils.data import Dataset
from utils.commonUtils import hash_qid, hash_did
from transformers import CLIPProcessor
from datasetUtils.load_dataset import *


def format_string(s):
    """Strip the string, remove carriage returns, and capitalize the first character."""
    s = (s or "").replace("\r", "").strip().strip('"')
    if s:  # If the string is not empty
        s = s[0].upper() + s[1:]  # Capitalize the first character
        s = s + "." if s[-1] not in [".", "?", "!"] else s  # Add a period at the end of the string
    return s


def parse_modality(modality):
    return 1 if modality == "image" else 0


class MBEIRMainDataset(Dataset):
    def __init__(self, config, is_train=True, onlyPrediction=False, testPrediction=False):
        super(MBEIRMainDataset, self).__init__()

        self.config = config
        self.random_config = self.config.FineTuning.NegativeCandidates
        self.is_train = is_train
        self.train_bs = self.config.FineTuning.Hyperparameters.TrainBatchSize
        self.onlyPrediction = onlyPrediction
        self.testPrediction = testPrediction

        self.load_query_instruction()
        Model = config.FineTuning.Model
        DataSet = config.Common.DataSet
        self.feature_processor = CLIPProcessor.from_pretrained(Model.Name, cache_dir=Model.CachePath)
        domains = DataSet.FilterDomains
        if is_train:
            self.ds_train = get_training_data(DataSet.Train, domains=domains)
            print("Training data loaded.")
            if self.random_config.UseModalityNegatives:
                self.load_modality_negatives(self.random_config.ModalityNegativesPath)
        else:
            if self.testPrediction:
                self.ds_test = get_test_data(domains=domains)
                print("Testing data loaded.")
            else:
                self.ds_validate = get_validation_data(DataSet.Test, domains=domains)
                print("Validation data loaded.")

        self._load_cand_pool_as_dict(DataSet.Candidate, domains=domains)
        print(f"{'Training' if self.is_train else ('Testing' if self.testPrediction else 'Validation')} candidates loaded.")


    def load_modality_negatives(self, path):
        if os.path.exists(path):
            with open(path, 'r') as f:
                modality_negatives = json.load(f)
            self.modality_negatives = modality_negatives
        else:
            raise Exception(f"Missing modality negatives file : {path}")

    def load_query_instruction(self):
        prompts_dict = {}
        with open(self.config.Common.DataSet.QueryInstructionsPath, "r") as f:
            next(f)  # Skip the header line
            for line in f.readlines():
                parts = line.strip().split("\t")
                # Construct the key to be dataset_id, query_modality, cand_modality
                key = f"{parts[3]}, {parts[0]}, {parts[1]}"
                prompts = [p for p in parts[4:] if p]  # Filters out any empty prompts
                prompts_dict[key] = prompts

        self.query_instructions = prompts_dict

    def _load_cand_pool_as_dict(self, perc, domains):

        cache_splits = "_".join(domains)

        if self.testPrediction:
            cache_name = f"cache/.cache_test_cand_{cache_splits}.json"
            det_name = f'cache/.cache_test_cand_count_{cache_splits}.json'
        else:
            cache_name = f"cache/.cache_cand_{cache_splits}.json"
            det_name = f'cache/.cache_cand_count_{cache_splits}.json'

        if os.path.exists(cache_name):

            with open(cache_name, 'r') as f:
                cand_pool_dict = json.load(f)

            with open(det_name, 'r') as f:
                candidate_det = json.load(f)

        else:
            cand_pool_dict = {}
            candidate_det = {f'{idx}': (0, 1e5, -1e5) for idx in range(10)}

            if self.testPrediction:
                ds_candidate = get_test_candidate_dataset(domains=domains)
            else:
                ds_candidate = get_candidate_dataset(perc, domains=domains)

            for cand_pool_entry in tqdm(ds_candidate, desc="Loading cand pool"):
                did = cand_pool_entry.get("did")
                cand_pool_dict[did] = cand_pool_entry
                det = did.split(':')
                dataset_id = det[0]
                doc_id = int(det[1])
                candidate_det[dataset_id] = (candidate_det[dataset_id][0] + 1,
                                             min(candidate_det[dataset_id][1], doc_id),
                                             max(candidate_det[dataset_id][2], doc_id))

            os.makedirs("cache/", exist_ok=True)
            with open(cache_name, 'w') as f:
                json.dump(cand_pool_dict, f)

            with open(det_name, 'w') as f:
                json.dump(candidate_det, f)

        self.cand_pool = cand_pool_dict
        self.candidate_det = candidate_det

    def _get_random_query_prompt(self, dataset_id, query_modality, cand_modality):
        key = f"{dataset_id}, {query_modality}, {cand_modality}"
        prompts = self.query_instructions.get(key, [])
        assert prompts, f"Cannot find prompts for {key}"
        prompt = format_string(random.choice(prompts))
        assert prompt, f"Prompt is empty for {key}"
        return prompt

    def __len__(self):
        if self.is_train:
            return len(self.ds_train)
        else:
            return len(self.ds_validate)

    def get_random_negative_candidate_dids(self, query_dataset_id, pos_cand_list, hard_negatives, augment=True):
        negative_candidate_dids = hard_negatives
        if self.random_config.CandidateSize > 0:
            if augment:
                tracker = self.random_config.CandidateSize - self.train_bs + 1 - len(negative_candidate_dids)
            else:
                tracker = self.random_config.CandidateSize
            choice_dataset_ids = [k for k, v in self.candidate_det.items() if v[0] > 0]
            while tracker > 0:
                if self.random_config.IncludeCrossDomainNegatives:
                    neg_dataset_id = random.choice(choice_dataset_ids)
                else:
                    neg_dataset_id = query_dataset_id
                doc_id_range = self.candidate_det[neg_dataset_id]
                neg_cand_did = neg_dataset_id + ':' + str(random.randint(doc_id_range[1], doc_id_range[2]))
                neg_cand = self.cand_pool.get(neg_cand_did, None)
                if neg_cand and neg_cand_did not in negative_candidate_dids and neg_cand_did not in pos_cand_list:
                    negative_candidate_dids.append(neg_cand_did)
                    tracker -= 1

        return negative_candidate_dids

    def get_modality_negatives(self, qid, pos_cand_list):
        negative_candidate_dids = []
        try:
            negative_candidates = [neg_did for neg_did in self.modality_negatives[qid] if neg_did not in pos_cand_list]
            available_negatives_len = len(negative_candidates)
            if available_negatives_len < self.random_config.CandidateSize:
                remaining = self.random_config.CandidateSize - available_negatives_len
                additional_negatives = self.get_random_negative_candidate_dids(
                    qid.split(":")[0], pos_cand_list, [], augment=False)
                negative_candidates += random.choices(additional_negatives, k=remaining)
        except Exception:
            negative_candidates = self.get_random_negative_candidate_dids(
                qid.split(":")[0], pos_cand_list, [], augment=False)

        if self.random_config.CandidateSize > 0:
            negative_candidate_dids = random.choices(negative_candidates, k=self.random_config.CandidateSize)

        return negative_candidate_dids

    def get_negative_candidate_dids(self, qid, pos_cand_list, hard_negatives):

        if self.random_config.UseModalityNegatives:
           return self.get_modality_negatives(qid, pos_cand_list)
        else:
            return self.get_random_negative_candidate_dids(qid.split(":")[0], pos_cand_list, hard_negatives)

    def _load_and_preprocess_image(self, query_img_path):
        """Load an image given a path"""
        if not query_img_path:
            return None
        image = Image.open(self.config.Common.DataSet.Path + '/' + query_img_path).convert("RGB")
        image = self.feature_processor(images=image, return_tensors='pt')["pixel_values"].squeeze(0)
        return image

    def _prepare_data_dict(self, txt, img_path):
        img = self._load_and_preprocess_image(img_path)
        return {"txt": txt, "img": img}

    def __getitem__(self, idx):
        if self.is_train:
            mbeir_entry = self.ds_train[idx]
        else:
            mbeir_entry = self.ds_validate[idx]

        query_txt = mbeir_entry.get("query_txt") or ""
        query_img_path = mbeir_entry.get("query_img_path", None)
        query_modality = mbeir_entry.get("query_modality", None)
        qid = mbeir_entry.get("qid", None)
        query_dataset_id = qid.split(":")[0] if qid else None

        pos_cand_list = mbeir_entry.get("pos_cand_list", [])

        selected_pos_cand_did = random.choice(pos_cand_list)
        pos_cand = self.cand_pool.get(selected_pos_cand_did)
        pos_cand_modality = pos_cand.get("modality", None)
        pos_cand_txt = pos_cand.get("txt") or ""
        pos_cand_txt = format_string(pos_cand_txt)

        query_prompt = self._get_random_query_prompt(query_dataset_id, query_modality, pos_cand_modality)
        query_txt_with_prompt = format_string(f"{query_prompt} {query_txt}")

        selected_neg_cand_list = []
        if self.is_train:
            hard_negatives = mbeir_entry.get("neg_cand_list", [])
            selected_neg_cand_id_list = self.get_negative_candidate_dids(qid, pos_cand_list, hard_negatives)
            for neg_cand_did in selected_neg_cand_id_list:
                neg_cand = self.cand_pool.get(neg_cand_did)
                neg_cand_txt = neg_cand.get("txt") or ""
                neg_cand_txt = format_string(neg_cand_txt)
                neg_cand["txt"] = neg_cand_txt
                selected_neg_cand_list.append(neg_cand)

        query = self._prepare_data_dict(query_txt_with_prompt, query_img_path)
        instance = {"query": query}

        pos_cand = self._prepare_data_dict(
            pos_cand_txt,
            pos_cand.get("img_path", None),
        )
        instance.update({"pos_cand": pos_cand})

        if not self.onlyPrediction:

            neg_cand_list = [
                self._prepare_data_dict(
                    neg_cand["txt"],
                    neg_cand.get("img_path", None),
                )
                for neg_cand in selected_neg_cand_list
            ]
            if len(neg_cand_list) > 0:
                instance.update({"neg_cand_list": neg_cand_list})

        else:
            instance.update({"qid": hash_qid(qid)})
            instance.update({"did": hash_did(selected_pos_cand_did)})
            remaining_pos_cands = []
            remaining_pos_cand_list = pos_cand_list[:]
            remaining_pos_cand_list.remove(selected_pos_cand_did)
            if len(remaining_pos_cand_list) > 0:
                for remaining_pos_cand_did in remaining_pos_cand_list:
                    rem_pos_cand = self.cand_pool.get(remaining_pos_cand_did)
                    rem_pos_cand_txt = rem_pos_cand.get("txt") or ""
                    rem_pos_cand_txt = format_string(rem_pos_cand_txt)
                    rem_pos_cand = self._prepare_data_dict(
                        rem_pos_cand_txt,
                        rem_pos_cand.get("img_path", None),
                    )
                    remaining_pos_cands.append(rem_pos_cand)

            instance.update({"remaining_pos_cand_list": remaining_pos_cands})
            instance.update({"remaining_did": [hash_did(remaining_did) for remaining_did in remaining_pos_cand_list]})

        if not self.is_train:
            instance["modality"] = parse_modality(pos_cand_modality)

        return instance


class MBEIRMainDatasetForTest(MBEIRMainDataset):
    def __init__(self, config):
        super(MBEIRMainDatasetForTest, self).__init__(config, is_train=False, onlyPrediction=True, testPrediction=True)

    def __len__(self):
        return len(self.ds_test)

    def __getitem__(self, idx):
        mbeir_entry = self.ds_test[idx]

        query_txt = mbeir_entry.get("query_txt") or ""
        query_img_path = mbeir_entry.get("query_img_path", None)
        query_modality = mbeir_entry.get("query_modality", None)
        qid = mbeir_entry.get("qid", None)
        query_dataset_id = qid.split(":")[0] if qid else None

        pos_cand_list = mbeir_entry.get("pos_cand_list", [])

        selected_pos_cand_did = random.choice(pos_cand_list)
        pos_cand = self.cand_pool.get(selected_pos_cand_did)
        pos_cand_modality = pos_cand.get("modality", None)
        pos_cand_txt = pos_cand.get("txt") or ""
        pos_cand_txt = format_string(pos_cand_txt)

        query_prompt = self._get_random_query_prompt(query_dataset_id, query_modality, pos_cand_modality)
        query_txt_with_prompt = format_string(f"{query_prompt} {query_txt}")

        query = self._prepare_data_dict(query_txt_with_prompt, query_img_path)
        instance = {"query": query}

        pos_cand = self._prepare_data_dict(
            pos_cand_txt,
            pos_cand.get("img_path", None),
        )
        instance.update({"pos_cand": pos_cand})

        instance.update({"qid": hash_qid(qid)})

        instance["modality"] = parse_modality(pos_cand_modality)

        return instance


class MBEIRMainDatasetForCandidate(Dataset):
    def __init__(self, config):
        super(MBEIRMainDatasetForCandidate, self).__init__()
        self.config = config
        domains = config.Common.DataSet.FilterDomains
        Model = config.Evaluate.Model
        self.feature_processor = CLIPProcessor.from_pretrained(Model.Name, cache_dir=Model.CachePath)
        self.ds_test_candidate = get_test_candidate_dataset(domains=domains)
        print(f"Candidates loaded")

    def __len__(self):
        return len(self.ds_test_candidate)

    def _load_and_preprocess_image(self, query_img_path):
        """Load an image given a path"""
        if not query_img_path:
            return None
        image = Image.open(self.config.Common.DataSet.Path + '/' + query_img_path).convert("RGB")
        image = self.feature_processor(images=image, return_tensors='pt')["pixel_values"].squeeze(0)
        return image

    def _prepare_data_dict(self, txt, img_path):
        img = self._load_and_preprocess_image(img_path)
        return {"txt": txt, "img": img}

    def __getitem__(self, idx):
        mbeir_entry = self.ds_test_candidate[idx]

        txt = mbeir_entry.get("txt") or ""
        txt = format_string(txt)
        img_path = mbeir_entry.get("img_path", None)
        did = mbeir_entry.get("did", None)

        instance = {
            "query": self._prepare_data_dict(txt, img_path),
            "did": hash_did(did),
        }
        return instance



