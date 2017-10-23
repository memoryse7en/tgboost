import copy_reg
import types
from multiprocessing import Pool
from functools import partial
import pandas as pd
import numpy as np


# use copy_reg to make the instance method picklable,
# because multiprocessing must pickle things to sling them among process
def _pickle_method(m):
    if m.im_self is None:
        return getattr, (m.im_class, m.im_func.func_name)
    else:
        return getattr, (m.im_self, m.im_func.func_name)

copy_reg.pickle(types.MethodType, _pickle_method)


class TreeNode(object):
    def __init__(self,is_leaf=False,leaf_score=None,feature=None,threshold=None,left_child=None,right_child=None,nan_direction=0):
        """
        :param is_leaf: if True, only need to initialize leaf_score. other params are for intermediate tree node
        :param leaf_score: prediction score of the leaf node
        :param feature: split feature of the intermediate node
        :param threshold: split threshold of the intermediate node
        :param left_child: left child node
        :param right_child: right child node
        :param nan_direction: if 0, those NAN sample goes to left child, if 1 goes to right child.
                              goes to left child by default
        """
        self.is_leaf = is_leaf
        self.feature = feature
        self.threshold = threshold
        self.nan_direction = nan_direction
        self.left_child = left_child
        self.right_child = right_child
        self.leaf_score = leaf_score


class Tree(object):
    def __init__(self):
        self.root = None
        self.min_sample_split = None
        self.colsample_bylevel = None
        self.reg_lambda = None
        self.gamma = None
        self.num_thread = None
        self.min_child_weight = None
        self.feature_importance = {}

    def calculate_leaf_score(self, Y):
        """
        According to xgboost, the leaf score is :
            - G / (H+lambda)
        """
        return - Y.grad.sum()/(Y.hess.sum()+self.reg_lambda)

    def calculate_split_gain(self, left_Y, right_Y, G_nan, H_nan, nan_direction=0):
        """
        According to xgboost, the scoring function is:
          gain = 0.5 * (GL^2/(HL+lambda) + GR^2/(HR+lambda) - (GL+GR)^2/(HL+HR+lambda)) - gamma

        this gain is the loss reduction, We want it to be as large as possible.

        G_nan, H_nan from NAN faeture value data, if nan_direction==0, they go to the left child.
        """
        GL = left_Y.grad.sum() + (1-nan_direction)*G_nan
        HL = left_Y.hess.sum() + (1-nan_direction)*H_nan
        GR = right_Y.grad.sum() + nan_direction*G_nan
        HR = right_Y.hess.sum() + nan_direction*H_nan
        gain = 0.5*(GL**2/(HL+self.reg_lambda) + GR**2/(HR+self.reg_lambda)
                    - (GL+GR)**2/(HL+HR+self.reg_lambda)) - self.gamma
        return gain

    def find_best_threshold(self, data, col):
        """
        :param data:
               the columns of the data: col, 'label', 'grad', 'hess'

        find best threshold for the given feature: col
        """
		#从data中选取指定feature(col)构造新的数据查找最佳分裂阈值
        selected_data = data[[col, 'label', 'grad', 'hess']]
        best_threshold = None
        best_gain = - np.inf
        nan_direction = 0

        # get the data with/without NAN feature value
        mask = selected_data[col].isnull()
        data_nan = selected_data[mask]
        G_nan = data_nan.grad.sum()
        H_nan = data_nan.hess.sum()
        data_not_nan = selected_data[~mask]

		#sort data by the selected feature
        data_not_nan.reset_index(inplace=True)
        data_not_nan.is_copy = False
        data_not_nan[col+'_idx'] = data_not_nan[col].argsort()
        data_not_nan = data_not_nan.ix[data_not_nan[col+'_idx']]

        # linear scan and find the best threshold
        for i in xrange(data_not_nan.shape[0]-1):
            #  not need to split at those same value
            cur_value, nxt_value = data_not_nan[col].iloc[i], data_not_nan[col].iloc[i+1]
            if cur_value == nxt_value:
                continue

            # split at this value
            this_threshold = (cur_value + nxt_value) / 2.0
            this_gain = None
            left_Y = data_not_nan.iloc[:(i+1)]
            right_Y = data_not_nan.iloc[(i+1):]

            # let the NAN data go to left and right, and chose the way which gets the max gain
            nan_goto_left_gain = self.calculate_split_gain(left_Y,right_Y,G_nan,H_nan,nan_direction=0)
            nan_goto_right_gain = self.calculate_split_gain(left_Y, right_Y, G_nan, H_nan, nan_direction=1)
            if nan_goto_left_gain < nan_goto_right_gain:
                cur_nan_direction = 1
                this_gain = nan_goto_right_gain
            else:
                cur_nan_direction = 0
                this_gain = nan_goto_left_gain

            if this_gain > best_gain:
                best_gain = this_gain
                best_threshold = this_threshold
				nan_direction = cur_nan_direction

        return col, best_threshold, best_gain, nan_direction

    def find_best_feature_threshold(self, X, Y):
        """
		para:
			X [selected_n_samples,selected_feature_samples]
			Y [selected_n_samples,5] column is [label,y_pred,grad,hess,sample_weight]

        find the (feature,threshold) with the largest gain
        if there are NAN in the feature, find its best direction to go
        """
        nan_direction = 0
        best_gain = - np.inf
        best_feature, best_threshold = None, None
        rsts = None

        # for each feature, find its best_threshold and best_gain, finally select the largest gain
        # implement in parallel
        cols = list(X.columns)
        data = pd.concat([X, Y], axis=1)
        func = partial(self.find_best_threshold, data)
        if self.num_thread == -1:
            pool = Pool()
            rsts = pool.map(func, cols)
            pool.close()

        else:
            pool = Pool(self.num_thread)
            rsts = pool.map(func, cols)
            pool.close()

        for rst in rsts:
            if rst[2] > best_gain:
                best_gain = rst[2]
                best_threshold = rst[1]
                best_feature = rst[0]
                nan_direction = rst[3]

        return best_feature, best_threshold, best_gain, nan_direction

    def split_dataset(self, X, Y, feature, threshold, nan_direction):
        """
        split the dataset according to (feature,threshold), nan_direction
            if faeture_value < feature_threshold, samples go to left child
            if faeture_value >= feature_threshold, samples go to right child
            if feature_value==NAN and nan_direction==0, samples go to left child.
            if feature_value==NAN and nan_direction==1, samples go to right child.
        """
        X_cols, Y_cols = list(X.columns), list(Y.columns)
        data = pd.concat([X, Y], axis=1)
        right_data, left_data = None, None
        if nan_direction == 0:
            mask = data[feature] >= threshold
            right_data = data[mask]
            left_data = data[~mask]
        else:
            mask = data[feature] < threshold
            right_data = data[~mask]
            left_data = data[mask]
        return left_data[X_cols], left_data[Y_cols], right_data[X_cols], right_data[Y_cols]

    def build(self, X, Y, max_depth):
        # check if min_sample_split or max_depth or min_child_weight satisfied
        if X.shape[0] < self.min_sample_split or max_depth == 0 or Y.hess.sum() < self.min_child_weight:
            is_leaf = True
            leaf_score = self.calculate_leaf_score(Y)
            return TreeNode(is_leaf=is_leaf, leaf_score=leaf_score)

        # column sample before splitting each tree node
        X_selected = X.sample(frac=self.colsample_bylevel, axis=1)

        # find the best feature(among the selected features) and its threshold to split
        best_feature, best_threshold, best_gain, nan_direction = self.find_best_feature_threshold(X_selected, Y)

        # if the gain is negative, it means the loss increase after splitting, so we stop split the tree node
        # node that xgboost does not stop, but adopt post pruning instead
        if best_gain < 0:
            is_leaf = True
            leaf_score = self.calculate_leaf_score(Y)
            return TreeNode(is_leaf=is_leaf, leaf_score=leaf_score)

        # if the gain is not negative, we split the data(original X) according to (best_feature,best_threshold) and nan_direction
        # then feed left data to left child, right data to right child
        # build the tree recursively
        left_X, left_Y, right_X, right_Y = self.split_dataset(X,Y,best_feature,best_threshold,nan_direction)
        left_tree = self.build(left_X, left_Y, max_depth-1)
        right_tree = self.build(right_X, right_Y, max_depth-1)

        # update the feature importance
        if self.feature_importance.has_key(best_feature):
            self.feature_importance[best_feature] += 1
        else:
            self.feature_importance[best_feature] = 0

        return TreeNode(is_leaf=False, leaf_score=None, feature=best_feature, threshold=best_threshold,
                        left_child=left_tree, right_child=right_tree, nan_direction=nan_direction)

    def fit(self, X, Y, max_depth=6, min_child_weight=1,
			colsample_bylevel=1.0, min_sample_split=10,
			reg_lambda=1.0, gamma=0.0, num_thread=-1):
		'''
		X:pd.DataFram [n_sampels,n_features]
		Y:pd.DataFram [n_samples,5],column is [label,y_pred,grad,hess,sample_weight]
		'''
        self.colsample_bylevel = colsample_bylevel
        self.min_sample_split = min_sample_split
        self.reg_lambda = reg_lambda
        self.gamma = gamma
        self.num_thread = num_thread
        self.min_child_weight = min_child_weight
        # build the tree by a recursive way
        self.root = self.build(X, Y, max_depth)

    def _predict(self, treenode, X):
        """
        predict a single sample
        note that X is a tupe(index,pandas.core.series.Series) from df.iterrows()
        """
        if treenode.is_leaf:
            return treenode.leaf_score
        elif pd.isnull(X[1][treenode.feature]):
            if treenode.nan_direction == 0:
                return self._predict(treenode.left_child, X)
            else:
                return self._predict(treenode.right_child, X)
        elif X[1][treenode.feature] < treenode.threshold:
            return self._predict(treenode.left_child, X)
        else:
            return self._predict(treenode.right_child, X)

    def predict(self, X):
        """
        predict multi samples
        X is pandas.core.frame.DataFrame
        """
        preds = None
        samples = X.iterrows()

        func = partial(self._predict, self.root)
        if self.num_thread == -1:
            pool = Pool()
            preds = pool.map(func, samples)
            pool.close()
        else:
            pool = Pool()
            preds = pool.map(func, samples)
            pool.close()
        return np.array(preds)
